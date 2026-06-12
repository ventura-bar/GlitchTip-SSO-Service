"""
Catch-all reverse-proxy handler.

Every request that is not handled by a specific auth route lands here.
The handler enforces SSO authentication and forwards approved requests to
the GlitchTip backend with a Bearer token injected from the proxy session.
"""
import json
import logging

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response

import store
from config import GLITCHTIP_URL, HEADERS_TO_DROP, SDK_PATH_RE
from .auth import keycloak_login_redirect

log = logging.getLogger("sso-proxy")
router = APIRouter()


def _is_sso_exempt(request: Request, path: str) -> bool:
    """
    Return True for requests that bypass SSO gate-keeping:
    - Sentry SDK event ingestion  (authenticated via DSN key in the URL)
    - Django admin panel          (authenticated via superuser session cookie)
    """
    has_sentry_auth = "x-sentry-auth" in {k.lower() for k in request.headers}
    is_admin = path in ("admin", "admin/") or path.startswith("admin/")
    return has_sentry_auth or SDK_PATH_RE.match(path) or is_admin


async def _load_session(request: Request) -> dict | None:
    """Return the parsed Redis session for this request, or None if missing/expired."""
    session_id = request.cookies.get("sso_session")
    if not session_id:
        return None
    raw = await store.redis.get(f"sso:session:{session_id}")
    return json.loads(raw) if raw else None


def _sso_return_path(request: Request, path: str) -> str:
    """
    Determine the return_path to store before sending the user to Keycloak.

    For normal pages the return path is the current URL.  For auth-flow pages
    (/login, /sso-*) the destination is meaningless as a post-SSO landing spot.
    We prefer, in order:
      1. ?next= query param  (Angular router sets this on unauthenticated deep-links)
      2. sso_return_hint cookie  (set when a session expires mid-session)
      3. "/" as a safe fallback
    """
    return_path = "/" + path
    if request.url.query:
        return_path += f"?{request.url.query}"

    if not (return_path.startswith("/login") or return_path.startswith("/sso")):
        return return_path

    for candidate in (request.query_params.get("next", ""), request.cookies.get("sso_return_hint", "")):
        if candidate and candidate.startswith("/") and not candidate.startswith("//"):
            return candidate
    return "/"


@router.api_route(
    "/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
)
async def proxy(request: Request, path: str = ""):
    """Authenticate the request and forward it to the GlitchTip backend."""
    if _is_sso_exempt(request, path):
        session = None
    else:
        # Execute a pending Keycloak logout before serving the next page.
        pending_logout = request.cookies.get("sso_pending_logout")
        if pending_logout and request.method == "GET":
            resp = RedirectResponse(pending_logout, status_code=302)
            resp.delete_cookie("sso_pending_logout")
            return resp

        session = await _load_session(request)
        if not session:
            return await keycloak_login_redirect(_sso_return_path(request, path))

        # Authenticated user hitting /login?next=<path> — the Angular router
        # redirected here because its auth guard fired before the async session
        # check completed.  Skip the login page and go straight to the destination;
        # the guard will pass because localStorage was primed by the SSO bridge page.
        if path == "login" and request.method == "GET":
            next_url = request.query_params.get("next", "")
            if next_url and next_url.startswith("/") and not next_url.startswith("//"):
                return RedirectResponse(next_url, status_code=302)

    # Forward to GlitchTip
    headers = {k: v for k, v in request.headers.items() if k.lower() not in HEADERS_TO_DROP}
    if session:
        headers["Authorization"] = f"Bearer {session['token']}"

    url = f"{GLITCHTIP_URL}/{path}"
    if request.url.query:
        url += f"?{request.url.query}"

    async with httpx.AsyncClient(follow_redirects=False, timeout=30) as client:
        upstream = await client.request(
            request.method, url,
            headers=headers,
            content=await request.body(),
        )

    resp_headers = {
        k: v for k, v in upstream.headers.items()
        if k.lower() not in HEADERS_TO_DROP
    }
    return Response(content=upstream.content, status_code=upstream.status_code, headers=resp_headers)
