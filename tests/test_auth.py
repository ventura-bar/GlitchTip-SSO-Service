"""
SSO authentication flow tests.

Each test starts with a fresh browser context (no cookies, no localStorage)
via the `fresh_page` fixture. Tests are fully independent.

Keycloak test credentials: username / password
  - alice    (groups: platform-team, backend-team)
  - nogroup  (no groups — created by the autouse session fixture in conftest)
"""
import re

from conftest import PROXY_URL, _PROXY_NETLOC, login


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_sso_session_id(page) -> str | None:
    for c in page.context.cookies():
        if c["name"] == "sso_session":
            return c["value"]
    return None


def _at_keycloak(page) -> bool:
    return "localhost:8180" in page.url


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestFreshLogin:
    """Tests for a brand-new browser session with no existing cookies or localStorage."""

    def test_with_groups_lands_on_org_issues(self, fresh_page):
        """
        Login with groups → proxy redirects to the first org's issues page.
        No /login flash: localStorage is primed by the bridge page before the
        Angular SPA boots.

        Keycloak returns groups in its own order (not necessarily realm.json order),
        so we match any org-team issues URL rather than a specific org.
        """
        final_url = login(fresh_page, "alice")
        assert re.search(r"/[a-z0-9-]+-team/issues/", final_url), (
            f"Expected to land on an org issues page, got: {final_url}"
        )
        assert "login" not in final_url

    def test_no_groups_lands_at_root(self, fresh_page):
        """
        A user with no SSO groups → proxy cannot pick an org, falls back to '/'.
        Must not crash or stay on Keycloak.
        """
        # ensure_nogroup_user is autouse=True so it runs automatically
        final_url = login(fresh_page, "nogroup")
        assert _PROXY_NETLOC in final_url, f"Still on Keycloak: {final_url}"
        assert "sso-callback" not in final_url
        assert not re.search(r"/[a-z0-9-]+-team/issues/", final_url), (
            f"No-group user should not land on an org page: {final_url}"
        )

    def test_localstorage_primed_before_spa_boots(self, fresh_page):
        """
        After the SSO callback, localStorage.isAuthenticated must be 'true' so
        the Angular auth guard passes synchronously on the very first render.
        """
        login(fresh_page, "alice")
        value = fresh_page.evaluate("() => localStorage.getItem('isAuthenticated')")
        assert value == "true", f"localStorage.isAuthenticated = {value!r}"


class TestSessionPersistence:
    """Tests for session state under various cookie and localStorage conditions."""

    def test_clear_localstorage_only_does_not_trigger_keycloak(self, fresh_page):
        """
        Clearing localStorage while the session cookie is still valid must NOT
        redirect to Keycloak. The proxy session is cookie-backed; localStorage
        is purely a client-side Angular hint.
        """
        login(fresh_page, "alice")
        fresh_page.evaluate("() => localStorage.clear()")

        fresh_page.goto(f"{PROXY_URL}/platform-team/issues/")
        fresh_page.wait_for_load_state("domcontentloaded")

        assert not _at_keycloak(fresh_page), (
            "Clearing localStorage alone should not trigger Keycloak re-auth"
        )

    def test_clear_all_cookies_and_localstorage_triggers_reauth(self, fresh_page):
        """
        Clearing cookies + localStorage (equivalent to 'Clear site data') leaves
        no session. The next navigation must redirect to the Keycloak login form.
        """
        login(fresh_page, "alice")

        fresh_page.context.clear_cookies()
        fresh_page.evaluate("() => { localStorage.clear(); sessionStorage.clear(); }")

        fresh_page.goto(PROXY_URL)
        fresh_page.wait_for_url("**/realms/glitchtip/**", timeout=10_000)
        assert _at_keycloak(fresh_page), "Should be redirected to Keycloak login"

    def test_expired_proxy_session_transparent_reauth(self, fresh_page, redis_client):
        """
        When the Redis session key is deleted (simulating TTL expiry), the proxy
        transparently re-authenticates via the live Keycloak SSO session — no login
        form is shown, and the user lands back on the requested page.
        """
        login(fresh_page, "alice")
        old_session_id = _get_sso_session_id(fresh_page)
        assert old_session_id

        redis_client.delete(f"sso:session:{old_session_id}")

        # Navigate to a protected resource — proxy detects missing session and
        # re-auths via Keycloak (transparent, since the Keycloak SSO cookie is alive).
        fresh_page.goto(f"{PROXY_URL}/platform-team/issues/")
        # Wait until the full cycle settles: proxy→Keycloak→sso-callback→bridge→destination
        fresh_page.wait_for_url(
            lambda url: (
                _PROXY_NETLOC in url
                and "sso-callback" not in url
                and "localhost:8180" not in url
            ),
            timeout=15_000,
        )

        assert not _at_keycloak(fresh_page), (
            "Expired session should be transparently renewed — Keycloak form must not appear"
        )
        assert _PROXY_NETLOC in fresh_page.url


class TestLoginPageHandling:
    """Tests for the proxy's interception of /login?next= requests."""

    def test_authenticated_user_login_next_skips_login_page(self, fresh_page):
        """
        GET /login?next=/platform-team/issues/ when already authenticated must
        redirect to the next URL without ever showing the login page.

        This covers the race where Angular's auth guard fires before the async
        /_allauth/session call resolves and sends the user to /login?next=...
        """
        login(fresh_page, "alice")

        fresh_page.goto(f"{PROXY_URL}/login?next=/platform-team/issues/")
        fresh_page.wait_for_load_state("domcontentloaded")

        assert "/platform-team/issues/" in fresh_page.url, (
            f"Authenticated user should bypass /login?next=, got: {fresh_page.url}"
        )
        assert "/login" not in fresh_page.url


class TestLogout:
    """Tests for the proxy's interception of DELETE /_allauth/session."""

    def test_logout_deletes_redis_session(self, fresh_page, redis_client):
        """Redis key must be gone immediately after the SPA sends DELETE."""
        login(fresh_page, "alice")
        session_id = _get_sso_session_id(fresh_page)
        assert session_id

        fresh_page.evaluate(
            "() => fetch('/_allauth/browser/v1/auth/session', {method: 'DELETE'})"
        )
        fresh_page.wait_for_timeout(500)

        assert redis_client.get(f"sso:session:{session_id}") is None, (
            "Redis session must be deleted immediately on logout"
        )

    def test_logout_redirects_to_keycloak_on_next_navigation(self, fresh_page):
        """
        After logout, the next page navigation must flush sso_pending_logout and
        redirect through Keycloak's end-session endpoint so the SSO session is
        also terminated.
        """
        login(fresh_page, "alice")

        fresh_page.evaluate(
            "() => fetch('/_allauth/browser/v1/auth/session', {method: 'DELETE'})"
        )
        fresh_page.wait_for_timeout(500)

        fresh_page.goto(PROXY_URL)
        fresh_page.wait_for_url("**/realms/glitchtip/**", timeout=10_000)
        assert _at_keycloak(fresh_page), "After logout, should be redirected to Keycloak"
