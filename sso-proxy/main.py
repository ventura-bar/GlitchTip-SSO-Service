import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import urllib.parse

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, Response

KEYCLOAK_URL = os.environ["KEYCLOAK_URL"].rstrip("/")
KEYCLOAK_EXTERNAL_URL = os.environ.get("KEYCLOAK_EXTERNAL_URL", KEYCLOAK_URL).rstrip("/")
KEYCLOAK_REALM = os.environ["KEYCLOAK_REALM"]
KEYCLOAK_CLIENT_ID = os.environ["KEYCLOAK_CLIENT_ID"]
KEYCLOAK_CLIENT_SECRET = os.environ["KEYCLOAK_CLIENT_SECRET"]
GLITCHTIP_URL = os.environ["GLITCHTIP_URL"].rstrip("/")
PROXY_BASE_URL = os.environ["PROXY_BASE_URL"].rstrip("/")
SESSION_SECRET = os.environ["SESSION_SECRET"]
ADMIN_TOKEN = os.environ["GLITCHTIP_PROXY_TOKEN"]

_REALM_INT = f"{KEYCLOAK_URL}/realms/{KEYCLOAK_REALM}/protocol/openid-connect"
_REALM_EXT = f"{KEYCLOAK_EXTERNAL_URL}/realms/{KEYCLOAK_REALM}/protocol/openid-connect"

# Sentry SDK ingestion paths — bypass SSO, authenticated via DSN key
_SDK_PATH = re.compile(r"^api/\d+/(store|envelope|minidump|security|unreal|attach)/")

_HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
})

_ALL_TOKEN_SCOPES = [
    "project:read", "project:write", "project:admin", "project:releases",
    "team:read", "team:write", "team:admin",
    "event:read", "event:write", "event:admin",
    "org:read", "org:write", "org:admin",
    "member:read", "member:write", "member:admin",
]

app = FastAPI()
sessions: dict[str, dict] = {}


def _derive_password(email: str) -> str:
    return hmac.new(SESSION_SECRET.encode(), email.encode(), hashlib.sha256).hexdigest()


async def _get_or_create_user_token(email: str) -> tuple[str, str, str]:
    """Returns (api_token, sessionid, csrftoken) from a live GlitchTip allauth session."""
    password = _derive_password(email)

    async with httpx.AsyncClient(base_url=GLITCHTIP_URL, follow_redirects=False) as client:
        # Get CSRF cookie
        await client.get("/_allauth/browser/v1/config")
        csrf = client.cookies.get("csrftoken", "")
        csrf_headers = {"X-CSRFToken": csrf, "Content-Type": "application/json"}

        # Try signup — allauth auto-logs-in on success (200)
        signup_resp = await client.post(
            "/_allauth/browser/v1/auth/signup",
            json={"email": email, "password": password},
            headers=csrf_headers,
        )
        csrf = client.cookies.get("csrftoken", csrf)
        csrf_headers["X-CSRFToken"] = csrf

        # If signup failed (user already exists), log in explicitly
        if signup_resp.status_code not in (200, 201):
            login_resp = await client.post(
                "/_allauth/browser/v1/auth/login",
                json={"email": email, "password": password},
                headers=csrf_headers,
            )
            if login_resp.status_code not in (200, 409):
                raise RuntimeError(f"GlitchTip login failed for {email}: {login_resp.status_code} {login_resp.text[:200]}")
            csrf = client.cookies.get("csrftoken", csrf)
            csrf_headers["X-CSRFToken"] = csrf

        sessionid = client.cookies.get("sessionid", "")

        # Return existing sso-proxy token if one exists, otherwise create one
        existing = await client.get("/api/0/api-tokens/", headers=csrf_headers)
        if existing.status_code == 200:
            for t in existing.json():
                if t.get("label") == "sso-proxy":
                    return t["token"], sessionid, csrf

        token_resp = await client.post(
            "/api/0/api-tokens/",
            json={"label": "sso-proxy", "scopes": _ALL_TOKEN_SCOPES},
            headers={"X-CSRFToken": csrf, "Content-Type": "application/json"},
        )
        token_resp.raise_for_status()
        csrf = client.cookies.get("csrftoken", csrf)
        return token_resp.json()["token"], sessionid, csrf


async def _sync_orgs(email: str, groups: list[str]) -> None:
    """Create GlitchTip orgs for each SSO group and add the user as admin member."""
    admin_headers = {"Authorization": f"Bearer {ADMIN_TOKEN}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(base_url=GLITCHTIP_URL) as client:
        for group in groups:
            slug = re.sub(r"[^a-z0-9]+", "-", group.lower()).strip("-")
            # Only create the org if it doesn't already exist
            check = await client.get(f"/api/0/organizations/{slug}/", headers=admin_headers)
            if check.status_code == 404:
                await client.post(
                    "/api/0/organizations/",
                    json={"name": group, "slug": slug},
                    headers=admin_headers,
                )
            # Invite the user — creates a pending OrganizationUser
            await client.post(
                f"/api/0/organizations/{slug}/members/",
                json={"email": email, "orgRole": "admin"},
                headers=admin_headers,
            )


@app.get("/sso-callback")
async def sso_callback(code: str):
    # Exchange authorization code for tokens
    async with httpx.AsyncClient(timeout=60) as client:
        token_resp = await client.post(
            f"{_REALM_INT}/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": f"{PROXY_BASE_URL}/sso-callback",
                "client_id": KEYCLOAK_CLIENT_ID,
                "client_secret": KEYCLOAK_CLIENT_SECRET,
            },
        )
        token_resp.raise_for_status()

    tokens = token_resp.json()

    # Decode JWT payload — trusted source, skip signature verification
    raw = tokens["access_token"].split(".")[1]
    raw += "=" * (-len(raw) % 4)
    claims = json.loads(base64.urlsafe_b64decode(raw))

    email: str = claims["email"]
    groups: list[str] = claims.get("groups", [])

    # Get or create a GlitchTip API token + capture the allauth session cookies
    user_token, gt_sessionid, gt_csrf = await _get_or_create_user_token(email)

    # Sync SSO groups → GlitchTip organizations
    await _sync_orgs(email, groups)

    session_id = secrets.token_urlsafe(32)
    sessions[session_id] = {
        "email": email,
        "token": user_token,
        "gt_sessionid": gt_sessionid,
        "gt_csrf": gt_csrf,
        "id_token": tokens.get("id_token", ""),
    }

    response = RedirectResponse("/", status_code=302)
    response.set_cookie("sso_session", session_id, httponly=True, samesite="lax")
    if gt_sessionid:
        response.set_cookie("sessionid", gt_sessionid, httponly=True, samesite="lax")
    if gt_csrf:
        response.set_cookie("csrftoken", gt_csrf, samesite="lax")
    return response


@app.api_route("/_allauth/browser/v1/auth/session", methods=["DELETE"])
async def logout(request: Request):
    """Intercept GlitchTip logout: clear proxy + GT session, redirect browser to Keycloak logout."""
    session_id = request.cookies.get("sso_session")
    session = sessions.pop(session_id, None) if session_id else None
    id_token = session.get("id_token", "") if session else ""

    # Build Keycloak logout URL — clears the Keycloak SSO session
    keycloak_logout_url = (
        f"{_REALM_EXT}/logout"
        f"?client_id={KEYCLOAK_CLIENT_ID}"
        f"&post_logout_redirect_uri={urllib.parse.quote(PROXY_BASE_URL + '/', safe='')}"
        + (f"&id_token_hint={id_token}" if id_token else "")
    )

    # Return allauth-compatible JSON so the SPA knows logout succeeded,
    # and set a cookie that tells the proxy to redirect to Keycloak logout on the next GET.
    response = Response(
        content='{"status":200,"data":{"user":null}}',
        status_code=200,
        media_type="application/json",
    )
    response.delete_cookie("sso_session")
    response.delete_cookie("sessionid")
    response.delete_cookie("csrftoken")
    # Stash the Keycloak logout URL so the next navigation clears the Keycloak session
    response.set_cookie("sso_pending_logout", keycloak_logout_url, httponly=True, samesite="lax", max_age=60)
    return response


@app.api_route(
    "/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
)
async def proxy(request: Request, path: str = ""):
    # SDK event ingestion: bypass session, forward as-is (auth via DSN key)
    is_sdk = (
        "x-sentry-auth" in {k.lower() for k in request.headers}
        or _SDK_PATH.match(path)
    )

    if not is_sdk:
        # If a pending Keycloak logout is queued, send the browser there first
        pending_logout = request.cookies.get("sso_pending_logout")
        if pending_logout and request.method == "GET":
            response = RedirectResponse(pending_logout, status_code=302)
            response.delete_cookie("sso_pending_logout")
            return response

        session_id = request.cookies.get("sso_session")
        session = sessions.get(session_id) if session_id else None
        if not session:
            params = urllib.parse.urlencode({
                "client_id": KEYCLOAK_CLIENT_ID,
                "redirect_uri": f"{PROXY_BASE_URL}/sso-callback",
                "response_type": "code",
                "scope": "openid email profile",
                "state": secrets.token_urlsafe(16),
            })
            return RedirectResponse(f"{_REALM_EXT}/auth?{params}")
    else:
        session = None

    headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in _HOP_BY_HOP | {"host"}
    }
    if session:
        headers["Authorization"] = f"Bearer {session['token']}"

    url = f"{GLITCHTIP_URL}/{path}"
    if request.url.query:
        url += f"?{request.url.query}"

    async with httpx.AsyncClient(follow_redirects=False, timeout=30) as client:
        resp = await client.request(
            request.method,
            url,
            headers=headers,
            content=await request.body(),
        )

    resp_headers = {
        k: v for k, v in resp.headers.items()
        if k.lower() not in _HOP_BY_HOP
    }
    return Response(content=resp.content, status_code=resp.status_code, headers=resp_headers)
