"""
SSO / authentication routes for the GlitchTip proxy.

Handles three concerns:
  - Starting the Keycloak OIDC flow  (keycloak_login_redirect)
  - Receiving the authorization-code callback  (sso_callback)
  - Intercepting GlitchTip's allauth session/logout endpoints
"""
import base64
import json
import logging
import re
import secrets
import time
import urllib.parse

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response

import store
from config import (
    KC_EXT,
    KC_INT,
    KEYCLOAK_CLIENT_ID,
    KEYCLOAK_CLIENT_SECRET,
    PROXY_BASE_URL,
    SESSION_TTL,
    STATE_TTL,
)
from glitchtip import accept_pending_invites, get_or_create_glitchtip_token, sync_orgs

log = logging.getLogger("sso-proxy")
router = APIRouter()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def keycloak_login_redirect(return_path: str = "/") -> RedirectResponse:
    """
    Build a redirect to Keycloak's authorization endpoint to start the OIDC flow.

    A random `state` token is stored in Redis with the intended return path so
    the callback can restore it after authentication.  Redis gives the state a
    short TTL (STATE_TTL) — long enough for a human to complete the Keycloak
    login page but short enough to limit replay risk.
    """
    if not return_path.startswith("/") or return_path.startswith("//"):
        return_path = "/"
    state = secrets.token_urlsafe(16)
    await store.redis.set(f"sso:state:{state}", return_path, ex=STATE_TTL, nx=True)
    params = urllib.parse.urlencode({
        "client_id":     KEYCLOAK_CLIENT_ID,
        "redirect_uri":  f"{PROXY_BASE_URL}/sso-callback",
        "response_type": "code",
        "scope":         "openid email profile",
        "state":         state,
    })
    return RedirectResponse(f"{KC_EXT}/auth?{params}")


# ---------------------------------------------------------------------------
# OIDC callback
# ---------------------------------------------------------------------------

@router.get("/sso-callback")
async def sso_callback(code: str, state: str = ""):
    """
    OIDC authorization-code callback (Keycloak redirects the browser here).

    Flow
    ────
    1. Validate the `state` parameter (CSRF protection).
    2. Exchange the authorization code for Keycloak tokens.
    3. Decode the JWT access-token to read email + group memberships.
    4. Provision the user's GlitchTip account and API token.
    5. Sync SSO groups → GlitchTip organizations; accept any pending invites.
    6. Open a Redis-backed proxy session and set the browser cookies.
    7. Return an HTML bridge page that primes localStorage before the redirect.

    Step 7 exists because GlitchTip's Angular auth guard reads
    `localStorage.isAuthenticated` *synchronously* at boot — before the async
    `/_allauth/session` call completes.  Without priming localStorage the guard
    sees `isAuthenticated = false` and bounces the user to the login page even
    though authentication just succeeded.  The bridge page sets the key and
    immediately calls `window.location.replace(return_path)` so the Angular
    app starts with the correct state.
    """
    # 1. Consume the state token atomically (GETDEL = get + delete in one round-trip).
    #    Returns the stored return_path, or None if the state is invalid/expired.
    return_path = await store.redis.getdel(f"sso:state:{state}")
    if return_path is None:
        return Response("Invalid or expired state — please try again.", status_code=400)

    try:
        # 2. Exchange the authorization code for tokens
        async with httpx.AsyncClient(timeout=60) as client:
            token_resp = await client.post(
                f"{KC_INT}/token",
                data={
                    "grant_type":    "authorization_code",
                    "code":          code,
                    "redirect_uri":  f"{PROXY_BASE_URL}/sso-callback",
                    "client_id":     KEYCLOAK_CLIENT_ID,
                    "client_secret": KEYCLOAK_CLIENT_SECRET,
                },
            )
            token_resp.raise_for_status()
        tokens = token_resp.json()

        # 3. Decode JWT (Keycloak TLS already validates the source; we trust the payload)
        raw = tokens["access_token"].split(".")[1]
        raw += "=" * (-len(raw) % 4) # CR: what is this for?
        claims: dict = json.loads(base64.urlsafe_b64decode(raw))
        email: str = claims["email"]
        groups: list[str] = claims.get("groups", [])
        log.info("SSO login: %s  groups=%s", email, groups)

        # When the return path is just "/" (e.g. fresh login from root), send the user
        # to their first org's issues page.  GlitchTip's Angular router uses
        # `/{orgSlug}/issues` — the `/organizations/` prefix is NOT a valid SPA route.
        if return_path == "/" and groups:
            first_slug = re.sub(r"[^a-z0-9]+", "-", groups[0].lower()).strip("-")
            return_path = f"/{first_slug}/issues/"

        # 4–5. Provision account and sync org memberships
        gt = await get_or_create_glitchtip_token(email)
        pending = await sync_orgs(email, groups)
        await accept_pending_invites(email, gt.user_id, pending)

        # 6. Create a Redis-backed proxy session (all shared state lives in Redis,
        #    so any number of proxy pods can serve subsequent requests).
        session_id = secrets.token_urlsafe(32)
        await store.redis.set(
            f"sso:session:{session_id}",
            json.dumps({
                "email":    email,
                "token":    gt.token,
                "user_id":  gt.user_id,
                "id_token": tokens.get("id_token", ""),
            }),
            ex=SESSION_TTL,
        )

        # CR: is this really the best way to do so?
        # 7. HTML bridge page — sets localStorage BEFORE Angular boots, then
        #    navigates to the return path without adding a history entry.
        safe_path = return_path.replace('"', "%22").replace("'", "%27")
        html = (
            "<!DOCTYPE html><html><head><script>"
            'localStorage.setItem("isAuthenticated","true");'
            f'window.location.replace("{safe_path}");'
            "</script></head><body></body></html>"
        )
        response = Response(content=html, media_type="text/html")
        response.set_cookie("sso_session", session_id, httponly=True, samesite="lax")
        if gt.sessionid:
            response.set_cookie("sessionid", gt.sessionid, httponly=True, samesite="lax")
        if gt.csrftoken:
            response.set_cookie("csrftoken", gt.csrftoken, samesite="lax")
        response.delete_cookie("sso_return_hint")
        return response

    except Exception:
        log.exception("SSO callback failed")
        return Response(
            "Login failed — please try again.  "
            "If the problem persists contact your administrator.",
            status_code=500,
            media_type="text/plain",
        )


# ---------------------------------------------------------------------------
# GlitchTip allauth session endpoint — intercepted by the proxy
# ---------------------------------------------------------------------------

@router.get("/_allauth/browser/v1/auth/session")
async def allauth_session_get(request: Request):
    """
    Return a synthetic allauth session response driven by the proxy session.

    GlitchTip's SPA calls this endpoint to decide whether the user is logged in.
    We answer from Redis rather than forwarding to Django because:
      - The Django session and the proxy session are created in the same SSO
        callback, but the browser's first API call can race the Django session
        becoming consistent (especially under load or across pods).
      - Redis is the single source of auth truth for the proxy layer.

    Unauthenticated response
    ────────────────────────
    When there is no valid proxy session we return HTTP 401 (what a real allauth
    endpoint returns for unauthenticated users — NOT a 200 with `user: null`).
    We also stash the current page in `sso_return_hint` so the proxy can restore
    it after the user re-authenticates.
    """
    session_id = request.cookies.get("sso_session")
    if session_id:
        raw = await store.redis.get(f"sso:session:{session_id}")
        if raw:
            session = json.loads(raw)
            email = session["email"]
            uid = int(session.get("user_id") or 0)
            payload = {
                "status": 200,
                "meta":   {"is_authenticated": True},
                "data": {
                    "user": {
                        "id":                  uid,
                        "display":             email,
                        "has_usable_password": True,
                        "email":               email,
                        "username":            email,
                    },
                    "methods": [{"method": "password", "at": time.time(), "email": email}],
                },
            }
            return Response(content=json.dumps(payload), media_type="application/json")

    # No valid session — capture the referer so we can restore the page after SSO.
    hint = "/"
    referer = request.headers.get("referer", "")
    if referer:
        parsed = urllib.parse.urlparse(referer)
        candidate = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        if (
            candidate.startswith("/")
            and not candidate.startswith("//")
            and not candidate.startswith("/login")
        ):
            hint = candidate

    resp = Response(
        content=(
            '{"status":401,"meta":{"is_authenticated":false},'
            '"data":{"flows":[{"id":"login"},{"id":"signup"}]}}'
        ),
        status_code=401,
        media_type="application/json",
    )
    if hint != "/":
        resp.set_cookie("sso_return_hint", hint, httponly=True, samesite="lax", max_age=300)
    return resp


# ---------------------------------------------------------------------------
# GlitchTip allauth logout endpoint — intercepted by the proxy
# ---------------------------------------------------------------------------

@router.api_route("/_allauth/browser/v1/auth/session", methods=["DELETE"])
async def logout(request: Request):
    """
    Intercept GlitchTip's logout call.

    Clears the proxy session from Redis, removes browser cookies, and queues a
    redirect to Keycloak's end-session endpoint so the SSO session is also
    terminated.

    The Keycloak redirect cannot happen immediately because the SPA expects a
    JSON response here.  We stash the logout URL in a short-lived cookie
    (`sso_pending_logout`) and execute it on the next browser navigation via
    the proxy catch-all handler.
    """
    session_id = request.cookies.get("sso_session")
    id_token = ""
    if session_id:
        raw = await store.redis.get(f"sso:session:{session_id}")
        if raw:
            id_token = json.loads(raw).get("id_token", "")
        await store.redis.delete(f"sso:session:{session_id}")

    kc_logout_url = (
        f"{KC_EXT}/logout"
        f"?client_id={KEYCLOAK_CLIENT_ID}"
        f"&post_logout_redirect_uri={urllib.parse.quote(PROXY_BASE_URL + '/', safe='')}"
        + (f"&id_token_hint={id_token}" if id_token else "")
    )

    response = Response(
        content='{"status":200,"data":{"user":null}}',
        media_type="application/json",
    )
    response.delete_cookie("sso_session")
    response.delete_cookie("sessionid")
    response.delete_cookie("csrftoken")
    response.set_cookie(
        "sso_pending_logout", kc_logout_url,
        httponly=True, samesite="lax", max_age=60,
    )
    return response
