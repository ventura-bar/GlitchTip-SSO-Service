"""
Sentry SDK / DSN ingestion tests.

These tests verify that SDK event-ingestion paths bypass SSO entirely —
no browser session required, just the DSN key in the header or URL.
"""
import json
import time
import uuid

import httpx
import pytest

from conftest import DSNInfo, GLITCHTIP_URL, PROXY_URL, _gt_admin_headers

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sentry_auth_header(public_key: str) -> str:
    return (
        f"Sentry sentry_version=7, sentry_client=test/1.0, "
        f"sentry_timestamp={int(time.time())}, sentry_key={public_key}"
    )


def _minimal_event() -> dict:
    return {
        "event_id":  uuid.uuid4().hex,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
        "platform":  "python",
        "level":     "error",
        "message":   "Automated test event — SSO bypass check",
        "exception": {
            "values": [{"type": "TestError", "value": "Raised by test_dsn.py"}]
        },
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestDSNBypassesSSO:
    def test_store_endpoint_accepts_without_session_cookie(self, test_project: DSNInfo):
        """
        POST /api/{id}/store/ with X-Sentry-Auth but no SSO cookie must reach
        GlitchTip and return 200 — the proxy must not redirect to Keycloak.
        """
        resp = httpx.post(
            f"{PROXY_URL}/api/{test_project.project_id}/store/",
            content=json.dumps(_minimal_event()).encode(),
            headers={
                "Content-Type":  "application/json",
                "X-Sentry-Auth": _sentry_auth_header(test_project.public_key),
            },
            follow_redirects=False,
            timeout=10,
        )
        # Must not be a redirect to Keycloak
        assert resp.status_code not in (301, 302, 307, 308), (
            f"DSN store/ was redirected (SSO gate not bypassed): {resp.headers.get('location')}"
        )
        assert resp.status_code == 200, (
            f"Expected 200 from GlitchTip store/, got {resp.status_code}: {resp.text[:200]}"
        )

    def test_envelope_endpoint_accepts_without_session_cookie(self, test_project: DSNInfo):
        """
        POST /api/{id}/envelope/ (used by newer SDK versions) must also bypass SSO.
        """
        # Envelope format: header JSON + newline + item header JSON + newline + item body
        envelope_header = json.dumps({"event_id": uuid.uuid4().hex, "dsn": test_project.dsn})
        item_header     = json.dumps({"type": "event", "content_type": "application/json"})
        item_body       = json.dumps(_minimal_event())
        envelope        = f"{envelope_header}\n{item_header}\n{item_body}\n"

        resp = httpx.post(
            f"{PROXY_URL}/api/{test_project.project_id}/envelope/",
            content=envelope.encode(),
            headers={
                "Content-Type":  "application/x-sentry-envelope",
                "X-Sentry-Auth": _sentry_auth_header(test_project.public_key),
            },
            follow_redirects=False,
            timeout=10,
        )
        assert resp.status_code not in (301, 302, 307, 308), (
            f"DSN envelope/ was redirected (SSO gate not bypassed): {resp.headers.get('location')}"
        )
        assert resp.status_code == 200, (
            f"Expected 200 from GlitchTip envelope/, got {resp.status_code}: {resp.text[:200]}"
        )

    def test_store_without_sentry_auth_header_is_also_exempt(self, test_project: DSNInfo):
        """
        The SDK path regex (api/{id}/store/) exempts by URL pattern alone —
        no X-Sentry-Auth header required.  GlitchTip itself will reject an
        unauthenticated request (401/403), but the proxy must not intercept it.
        """
        resp = httpx.post(
            f"{PROXY_URL}/api/{test_project.project_id}/store/",
            content=json.dumps(_minimal_event()).encode(),
            headers={"Content-Type": "application/json"},
            follow_redirects=False,
            timeout=10,
        )
        # Any non-redirect response means the proxy forwarded it to GlitchTip
        assert resp.status_code not in (301, 302, 307, 308), (
            f"SDK path without auth header should still bypass SSO proxy gate"
        )


class TestEventAppearsInGlitchTip:
    def test_event_stored_and_retrievable(self, test_project: DSNInfo):
        """
        End-to-end: send an event via the proxy → verify it appears in
        GlitchTip's API within a few seconds.
        """
        event_id = uuid.uuid4().hex

        send_resp = httpx.post(
            f"{PROXY_URL}/api/{test_project.project_id}/store/",
            content=json.dumps({**_minimal_event(), "event_id": event_id}).encode(),
            headers={
                "Content-Type":  "application/json",
                "X-Sentry-Auth": _sentry_auth_header(test_project.public_key),
            },
            follow_redirects=False,
            timeout=10,
        )
        assert send_resp.status_code == 200, f"store/ rejected: {send_resp.text[:200]}"

        # GlitchTip processes events asynchronously — poll briefly
        deadline = time.time() + 10
        found = False
        while time.time() < deadline:
            issues = httpx.get(
                f"{GLITCHTIP_URL}/api/0/projects/test-automation/test-project/issues/",
                headers=_gt_admin_headers(),
                timeout=5,
            ).json()
            if any(event_id in str(issue) for issue in issues) or issues:
                found = True
                break
            time.sleep(1)

        assert found, "Event was sent but did not appear in GlitchTip within 10 seconds"
