import hashlib
import hmac
import logging
import re
import urllib.parse
from dataclasses import dataclass

import httpx

import store
from config import (
    ADMIN_TOKEN,
    ALL_TOKEN_SCOPES,
    DJANGO_ADMIN_EMAIL,
    DJANGO_ADMIN_PASSWORD,
    DJANGO_ADMIN_ROLE_ADMIN,
    GLITCHTIP_URL,
    SESSION_SECRET,
)

log = logging.getLogger("sso-proxy")


@dataclass
class GlitchTipAuth:
    token: str
    sessionid: str
    csrftoken: str
    user_id: str


def _group_to_slug(group: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", group.lower()).strip("-") # CR: What is the name convention here? Is this just a URL slug or does GlitchTip have specific requirements for org slugs?

def _derive_password(email: str) -> str:
    """Deterministic per-user password for proxy-managed GlitchTip accounts."""
    return hmac.digest(SESSION_SECRET.encode(), email.encode(), hashlib.sha256).hex()


async def get_or_create_glitchtip_token(email: str) -> GlitchTipAuth:
    """
    Sign in to GlitchTip as *email* (creating the account on first login) and
    return auth details.

    GlitchTip has no SSO bridge, so the proxy maintains a shadow account for
    each SSO user with a deterministic password derived from SESSION_SECRET.
    """
    password = _derive_password(email)
    log.info("provisioning GlitchTip session for %s", email)

    async with httpx.AsyncClient(base_url=GLITCHTIP_URL, follow_redirects=False) as client:

        # Step 1 — get a CSRF token
        await client.get("/_allauth/browser/v1/config")
        csrf = client.cookies.get("csrftoken", "")

        # Step 2 — sign up (first login) or log in (returning user)
        signup = await client.post(
            "/_allauth/browser/v1/auth/signup",
            json={"email": email, "password": password},
            headers={"X-CSRFToken": csrf, "Content-Type": "application/json"},
        )
        csrf = client.cookies.get("csrftoken", csrf)

        if signup.status_code not in (200, 201):
            login = await client.post(
                "/_allauth/browser/v1/auth/login",
                json={"email": email, "password": password},
                headers={"X-CSRFToken": csrf, "Content-Type": "application/json"},
            )
            if login.status_code not in (200, 409):
                raise RuntimeError(
                    f"GlitchTip login failed for {email}: "
                    f"{login.status_code} {login.text[:200]}"
                )
            csrf = client.cookies.get("csrftoken", csrf)

        sessionid = client.cookies.get("sessionid", "")

        # Step 3 — get the user's GlitchTip ID (needed to accept invites via admin)
        me = await client.get("/api/0/users/me/", headers={"X-CSRFToken": csrf})
        user_id = str(me.json().get("id", ""))

        # Step 4 — reuse an existing sso-proxy API token if one already exists
        existing = await client.get(
            "/api/0/api-tokens/",
            headers={"X-CSRFToken": csrf, "Content-Type": "application/json"},
        )
        if existing.status_code == 200:
            for token in existing.json():
                if token.get("label") == "sso-proxy":
                    return GlitchTipAuth(token["token"], sessionid, csrf, user_id)

        # Step 5 — create a new full-scope API token
        resp = await client.post(
            "/api/0/api-tokens/",
            json={"label": "sso-proxy", "scopes": ALL_TOKEN_SCOPES},
            headers={"X-CSRFToken": csrf, "Content-Type": "application/json"},
        )
        resp.raise_for_status()
        csrf = client.cookies.get("csrftoken", csrf)
        log.info("created new API token for %s", email)
        return GlitchTipAuth(resp.json()["token"], sessionid, csrf, user_id)


async def sync_orgs(email: str, groups: list[str]) -> list[tuple[str, str]]:
    """
    Ensure a GlitchTip org exists for each SSO group and invite the user as admin.
    Returns a list of (member_id, org_id) pairs for all newly created pending invites.
    """
    admin_headers = {"Authorization": f"Bearer {ADMIN_TOKEN}", "Content-Type": "application/json"}
    pending: list[tuple[str, str]] = []

    async with httpx.AsyncClient(base_url=GLITCHTIP_URL) as client:
        for group in groups:
            slug = _group_to_slug(group)

            org_resp = await client.get(f"/api/0/organizations/{slug}/", headers=admin_headers)
            if org_resp.status_code == 404:
                log.info("creating org '%s'", slug)
                org_resp = await client.post(
                    "/api/0/organizations/",
                    json={"name": group, "slug": slug},
                    headers=admin_headers,
                )
            org_resp.raise_for_status()
            org_id = str(org_resp.json()["id"])

            invite = await client.post(
                f"/api/0/organizations/{slug}/members/",
                json={"email": email, "orgRole": "admin"},
                headers=admin_headers,
            )
            if invite.status_code in (200, 201):
                pending.append((str(invite.json()["id"]), org_id))
            else:
                log.info("%s is already a member of '%s'", email, slug)

    return pending


async def accept_pending_invites(email: str, user_id: str, pending: list[tuple[str, str]]) -> None:
    """
    Accept pending org invites by updating each OrganizationUser record via the
    Django admin. GlitchTip's REST API creates invites with user=NULL and offers
    no endpoint to resolve them — the admin change form is the only API available.
    """
    if not pending:
        return

    async with httpx.AsyncClient(base_url=GLITCHTIP_URL, follow_redirects=True) as client:

        # Authenticate as superuser via allauth
        await client.get("/_allauth/browser/v1/config")
        csrf = client.cookies.get("csrftoken", "")
        await client.post(
            "/_allauth/browser/v1/auth/login",
            json={"email": DJANGO_ADMIN_EMAIL, "password": DJANGO_ADMIN_PASSWORD},
            headers={"X-CSRFToken": csrf, "Content-Type": "application/json"},
        )
        csrf = client.cookies.get("csrftoken", csrf)

        for member_id, org_id in pending:
            resp = await client.post(
                f"/admin/organizations_ext/organizationuser/{member_id}/change/",
                content=urllib.parse.urlencode({
                    "csrfmiddlewaretoken": csrf,
                    "organization": org_id,
                    "user": user_id,
                    "email": email,
                    "role": DJANGO_ADMIN_ROLE_ADMIN,
                    "_save": "Save",
                }).encode(),
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Referer": f"{GLITCHTIP_URL}/admin/organizations_ext/organizationuser/{member_id}/change/",
                },
            )
            # Django admin redirects to the changelist on success; staying on
            # the change page means the form was rejected with validation errors.
            if f"/{member_id}/change/" not in str(resp.url):
                log.info("%s accepted into org (member %s)", email, member_id)
            else:
                log.warning("admin accept failed for %s (member %s)", email, member_id)
