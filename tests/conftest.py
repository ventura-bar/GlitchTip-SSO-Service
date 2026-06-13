"""
Shared fixtures and helpers for the GlitchTip SSO proxy test suite.

Run from the project root:
    cd tests && pip install -r requirements.txt && playwright install chromium
    pytest -v
"""
import os
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx
import pytest
import redis as redis_lib
from dotenv import load_dotenv
from playwright.sync_api import Page

# ── Load environment ───────────────────────────────────────────────────────────

load_dotenv(Path(__file__).parent.parent / ".env")

# Override any of these via env vars to run against a kind/remote deployment:
#   PROXY_URL=http://localhost:8888 GLITCHTIP_URL=http://localhost:8003 pytest
PROXY_URL      = os.environ.get("PROXY_URL",      "http://localhost:8090")
# GLITCHTIP_URL should be a direct URL to GlitchTip (bypasses sso-proxy).
# In docker-compose: http://localhost:8000
# In kind: port-forward the glitchtip-web service, e.g. http://localhost:8003
GLITCHTIP_URL  = os.environ.get("GLITCHTIP_URL",  "http://localhost:8000")
KEYCLOAK_URL   = os.environ.get("KEYCLOAK_URL",   "http://localhost:8180")
REDIS_URL      = os.environ.get("REDIS_URL",      "redis://localhost:6379")
KC_REALM       = os.environ.get("KC_REALM",       "glitchtip")
ADMIN_TOKEN    = os.environ["GLITCHTIP_PROXY_TOKEN"]
KC_ADMIN_PASS  = os.environ["KEYCLOAK_ADMIN_PASSWORD"]

# Netloc of the proxy (host:port), used to detect when the browser has landed
# back on the proxy after the SSO callback.
_PROXY_NETLOC = urlparse(PROXY_URL).netloc


# ── Low-level helpers ──────────────────────────────────────────────────────────

def _kc_admin_token() -> str:
    """Obtain a short-lived Keycloak master-realm admin token."""
    resp = httpx.post(
        f"{KEYCLOAK_URL}/realms/master/protocol/openid-connect/token",
        data={
            "grant_type": "password",
            "client_id":  "admin-cli",
            "username":   "admin",
            "password":   KC_ADMIN_PASS,
        },
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def _gt_admin_headers() -> dict:
    return {"Authorization": f"Bearer {ADMIN_TOKEN}", "Content-Type": "application/json"}


def login(page: Page, username: str = "alice", password: str = "password") -> str:
    """
    Drive the browser through the full SSO flow and return the final URL.

    The browser starts from the proxy root, gets redirected to Keycloak, fills
    the login form, then waits for the proxy's localStorage bridge page to
    execute and navigate to the final destination.
    """
    page.goto(PROXY_URL)
    page.wait_for_url(f"**/realms/{KC_REALM}/**", timeout=10_000)
    page.fill("#username", username)
    page.fill("#password", password)
    page.click("#kc-login")
    # Wait until the bridge page has redirected us away from /sso-callback
    page.wait_for_url(
        lambda url: _PROXY_NETLOC in url and "sso-callback" not in url,
        timeout=15_000,
    )
    return page.url


# ── Session-scoped setup fixtures ──────────────────────────────────────────────

@pytest.fixture(scope="session", autouse=True)
def ensure_nogroup_user():
    """
    Create a Keycloak user with no group memberships for the no-groups test.
    Idempotent: skipped if the user already exists.
    """
    token = _kc_admin_token()
    headers = {"Authorization": f"Bearer {token}"}
    base = f"{KEYCLOAK_URL}/admin/realms/{KC_REALM}"

    existing = httpx.get(f"{base}/users?username=nogroup", headers=headers, timeout=10)
    existing.raise_for_status()
    if existing.json():
        return  # already exists

    resp = httpx.post(
        f"{base}/users",
        json={
            "username":      "nogroup",
            "email":         "nogroup@example.com",
            "firstName":     "No",
            "lastName":      "Group",
            "enabled":       True,
            "emailVerified": True,
            "credentials":   [{"type": "password", "value": "password", "temporary": False}],
        },
        headers={**headers, "Content-Type": "application/json"},
        timeout=10,
    )
    resp.raise_for_status()


@dataclass
class DSNInfo:
    project_id: int
    public_key: str
    dsn: str


@pytest.fixture(scope="session")
def test_project() -> DSNInfo:
    """
    Ensure a GlitchTip organization + project exist for DSN ingestion tests.
    Returns DSN details. Idempotent.
    """
    headers = _gt_admin_headers()
    org_slug = "test-automation"

    # Create org if missing
    org_resp = httpx.get(f"{GLITCHTIP_URL}/api/0/organizations/{org_slug}/", headers=headers, timeout=10)
    if org_resp.status_code == 404:
        org_resp = httpx.post(
            f"{GLITCHTIP_URL}/api/0/organizations/",
            json={"name": "Test Automation", "slug": org_slug},
            headers=headers,
            timeout=10,
        )
    org_resp.raise_for_status()

    # Create a team (required before creating a project) — check first to stay idempotent
    existing_teams = httpx.get(
        f"{GLITCHTIP_URL}/api/0/organizations/{org_slug}/teams/",
        headers=headers, timeout=10,
    )
    team_slugs = {t["slug"] for t in existing_teams.json()} if existing_teams.status_code == 200 else set()
    if "test-team" not in team_slugs:
        team_resp = httpx.post(
            f"{GLITCHTIP_URL}/api/0/organizations/{org_slug}/teams/",
            json={"name": "Test Team", "slug": "test-team"},
            headers=headers,
            timeout=10,
        )
        team_resp.raise_for_status()

    # Create project if missing
    projects = httpx.get(
        f"{GLITCHTIP_URL}/api/0/organizations/{org_slug}/projects/",
        headers=headers,
        timeout=10,
    ).json()
    project = next((p for p in projects if p["slug"] == "test-project"), None)

    if not project:
        proj_resp = httpx.post(
            f"{GLITCHTIP_URL}/api/0/teams/{org_slug}/test-team/projects/",
            json={"name": "Test Project", "slug": "test-project", "platform": "python"},
            headers=headers,
            timeout=10,
        )
        proj_resp.raise_for_status()
        project = proj_resp.json()

    # Get DSN
    keys_resp = httpx.get(
        f"{GLITCHTIP_URL}/api/0/projects/{org_slug}/test-project/keys/",
        headers=headers,
        timeout=10,
    )
    keys_resp.raise_for_status()
    key = keys_resp.json()[0]

    project_id = project["id"]
    public_key = key["public"]
    # Rewrite DSN host to go through the proxy
    dsn = key["dsn"]["public"].replace(GLITCHTIP_URL, PROXY_URL)

    return DSNInfo(project_id=project_id, public_key=public_key, dsn=dsn)


# ── Per-test fixtures ──────────────────────────────────────────────────────────

@pytest.fixture
def redis_client():
    """Sync Redis client for direct session manipulation."""
    r = redis_lib.Redis.from_url(REDIS_URL, decode_responses=True)
    yield r
    r.close()


@pytest.fixture
def fresh_page(browser):
    """A browser page with a completely fresh context (no cookies, no storage)."""
    context = browser.new_context()
    page = context.new_page()
    yield page
    context.close()
