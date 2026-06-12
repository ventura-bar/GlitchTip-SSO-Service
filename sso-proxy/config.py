import os
import re

# ── Environment ────────────────────────────────────────────────────────────────

KEYCLOAK_URL           = os.environ["KEYCLOAK_URL"].rstrip("/")
KEYCLOAK_EXTERNAL_URL  = os.environ.get("KEYCLOAK_EXTERNAL_URL", KEYCLOAK_URL).rstrip("/")
KEYCLOAK_REALM         = os.environ["KEYCLOAK_REALM"]
KEYCLOAK_CLIENT_ID     = os.environ["KEYCLOAK_CLIENT_ID"]
KEYCLOAK_CLIENT_SECRET = os.environ["KEYCLOAK_CLIENT_SECRET"]

GLITCHTIP_URL = os.environ["GLITCHTIP_URL"].rstrip("/")
ADMIN_TOKEN   = os.environ["GLITCHTIP_PROXY_TOKEN"]

PROXY_BASE_URL        = os.environ["PROXY_BASE_URL"].rstrip("/")
SESSION_SECRET        = os.environ["SESSION_SECRET"]
REDIS_URL             = os.environ["REDIS_URL"]
DJANGO_ADMIN_EMAIL    = os.environ["DJANGO_SUPERUSER_EMAIL"]
DJANGO_ADMIN_PASSWORD = os.environ["DJANGO_SUPERUSER_PASSWORD"]

for _name, _val, _placeholder in [
    ("SESSION_SECRET",        SESSION_SECRET,        "change_me_to_something_random"),
    ("ADMIN_TOKEN",           ADMIN_TOKEN,           "glitchtip-proxy-admin-token-change-me-in-prod"),
    ("DJANGO_ADMIN_PASSWORD", DJANGO_ADMIN_PASSWORD, "admin123"),
]:
    if _val == _placeholder:
        raise RuntimeError(
            f"{_name} is still set to its placeholder value — set a real secret before starting"
        )

# ── Constants ──────────────────────────────────────────────────────────────────

KC_INT = f"{KEYCLOAK_URL}/realms/{KEYCLOAK_REALM}/protocol/openid-connect"
KC_EXT = f"{KEYCLOAK_EXTERNAL_URL}/realms/{KEYCLOAK_REALM}/protocol/openid-connect"

# Sentry SDK ingestion paths — carry a DSN key, no SSO needed
SDK_PATH_RE = re.compile(r"^api/\d+/(store|envelope|minidump|security|unreal|attach)/")

# Headers stripped before forwarding to the upstream or returning to the client
HEADERS_TO_DROP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
    "host",  # rewritten by httpx for the upstream request
})

# Role value for "Admin" as expected by the Django admin change form
DJANGO_ADMIN_ROLE_ADMIN = "1"

ALL_TOKEN_SCOPES = [
    "project:read", "project:write", "project:admin", "project:releases",
    "team:read",    "team:write",    "team:admin",
    "event:read",   "event:write",   "event:admin",
    "org:read",     "org:write",     "org:admin",
    "member:read",  "member:write",  "member:admin",
]

STATE_TTL   = 600    # 10 minutes — OIDC state lifetime
SESSION_TTL = 28800  # 8 hours    — proxy session lifetime
