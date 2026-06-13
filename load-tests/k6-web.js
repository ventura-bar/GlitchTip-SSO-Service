/**
 * k6-web.js — Authenticated GlitchTip dashboard browsing load test
 *
 * Simulates users navigating the GlitchTip dashboard through the SSO proxy.
 * Authenticates via the full OIDC authorization-code flow:
 *   1. GET proxy root → redirected to Keycloak login page
 *   2. POST credentials → Keycloak redirects to /sso-callback
 *   3. GET /sso-callback → proxy creates Redis session, sets sso_session cookie
 *   4. Subsequent requests carry the cookie — proxy forwards them to GlitchTip
 *
 * This correctly tests the proxy's web-API path (not just DSN bypass).
 *
 * Run:
 *   PROXY_URL=http://localhost:8888 k6 run load-tests/k6-web.js
 *
 * Environment variables:
 *   PROXY_URL     — default http://localhost:8090
 *   TEST_USERNAME — default alice
 *   TEST_PASSWORD — default password
 */
import http from "k6/http";
import { check, sleep } from "k6";
import { Trend, Rate } from "k6/metrics";

// ── Config ────────────────────────────────────────────────────────────────────

const PROXY_URL     = __ENV.PROXY_URL     || "http://localhost:8090";
const TEST_USERNAME = __ENV.TEST_USERNAME || "alice";
const TEST_PASSWORD = __ENV.TEST_PASSWORD || "password";

// ── Custom metrics ────────────────────────────────────────────────────────────

const pageLatency = new Trend("web_page_latency_ms", true);
const authLatency = new Trend("web_auth_latency_ms", true);
const pageSuccess = new Rate("web_page_success");

// ── Test options ──────────────────────────────────────────────────────────────

export const options = {
  scenarios: {
    dashboard_browsing: {
      executor: "ramping-vus",
      startVUs: 0,
      stages: [
        { duration: "30s", target: 20  },
        { duration: "60s", target: 100 },
        { duration: "30s", target: 200 },
        { duration: "60s", target: 200 },
        { duration: "30s", target: 0   },
      ],
    },
  },
  thresholds: {
    // API page latency — all three dashboard calls must be under 2s at p95
    web_page_latency_ms: ["p(95)<2000"],
    // Only count failures on API requests (tagged web_orgs/web_projects/web_issues),
    // not OIDC auth requests which can fail under VU startup bursts
    "http_req_failed{name:web_orgs}":     ["rate<0.02"],
    "http_req_failed{name:web_projects}": ["rate<0.02"],
    "http_req_failed{name:web_issues}":   ["rate<0.02"],
    // Overall page success rate
    web_page_success: ["rate>0.95"],
  },
};

// ── OIDC authorization-code flow ──────────────────────────────────────────────

/**
 * Perform the full OIDC browser flow through the proxy.
 * Uses the VU's default cookie jar so the sso_session cookie persists
 * automatically in all subsequent requests from this VU.
 *
 * Returns true on success, false on failure.
 */
function oidcLogin(username, password) {
  const t0 = Date.now();

  // 1. Hit proxy root — it redirects (302×N) to Keycloak's login page.
  //    k6's default redirects=10 follows all hops, ending at the Keycloak form.
  const kcRes = http.get(PROXY_URL + "/", {
    tags: { name: "oidc_login_page" },
  });

  if (!kcRes.url.includes("realms/")) {
    console.error(`Expected Keycloak login page, got: ${kcRes.url} (${kcRes.status})`);
    return false;
  }

  // 2. Parse the login form action URL from Keycloak's response HTML.
  const match = kcRes.body.match(/action="([^"]+)"/);
  if (!match) {
    console.error("Could not parse Keycloak login form action");
    return false;
  }
  const formAction = match[1].replace(/&amp;/g, "&");

  // 3. POST credentials. Keycloak validates them and issues a 302 to /sso-callback.
  //    redirects:0 lets us capture the callback URL before following it.
  const loginRes = http.post(
    formAction,
    { username, password },
    {
      redirects: 0,
      tags: { name: "oidc_credentials_post" },
    },
  );

  if (loginRes.status !== 302) {
    console.error(`Credential POST returned ${loginRes.status} — check username/password`);
    return false;
  }

  const callbackUrl = loginRes.headers["Location"];
  if (!callbackUrl || !callbackUrl.includes("sso-callback")) {
    console.error(`Unexpected redirect after login: ${callbackUrl}`);
    return false;
  }

  // 4. Follow /sso-callback. The proxy exchanges the authorization code for
  //    tokens, creates a Redis session, and sets the sso_session cookie.
  //    redirects:0 so we stop at the bridge HTML page (JS redirect not followed).
  const cbRes = http.get(callbackUrl, {
    redirects: 0,
    tags: { name: "oidc_sso_callback" },
  });

  if (cbRes.status !== 200) {
    console.error(`/sso-callback returned ${cbRes.status}: ${cbRes.body.substring(0, 200)}`);
    return false;
  }

  authLatency.add(Date.now() - t0);

  // Verify the cookie landed in the default jar
  const cookies = http.cookieJar().cookiesForURL(PROXY_URL + "/");
  if (!(cookies["sso_session"] || []).length) {
    console.error("sso_session cookie not found after OIDC callback");
    return false;
  }

  return true;
}

// ── Per-VU state ──────────────────────────────────────────────────────────────

let authenticated = false;
let orgSlug       = null;

// ── Main scenario ─────────────────────────────────────────────────────────────

export default function () {
  // Authenticate once per VU (re-authenticate if session was cleared)
  if (!authenticated) {
    authenticated = oidcLogin(TEST_USERNAME, TEST_PASSWORD);
    if (!authenticated) {
      sleep(2);
      return;
    }
  }

  // All requests below use k6's default cookie jar, which holds the sso_session
  // cookie set during oidcLogin. The proxy reads this cookie to identify the user.

  // Page 1: List organizations
  const orgsRes = http.get(
    `${PROXY_URL}/api/0/organizations/`,
    { tags: { name: "web_orgs" } },
  );
  pageLatency.add(orgsRes.timings.duration);

  // Detect session expiry: proxy redirects to Keycloak instead of serving JSON
  if (orgsRes.url.includes("realms/")) {
    authenticated = false;
    sleep(1);
    return;
  }

  const orgsOk = check(orgsRes, {
    "orgs 200 JSON": (r) => r.status === 200 && r.body.startsWith("["),
  });
  pageSuccess.add(orgsOk ? 1 : 0);

  if (!orgsOk) {
    sleep(1);
    return;
  }

  if (!orgSlug) {
    orgSlug = orgsRes.json()[0].slug;
  }

  sleep(Math.random() * 2 + 1);  // think time: 1–3s

  // Page 2: List projects
  const projRes = http.get(
    `${PROXY_URL}/api/0/organizations/${orgSlug}/projects/`,
    { tags: { name: "web_projects" } },
  );
  pageLatency.add(projRes.timings.duration);
  pageSuccess.add(check(projRes, { "projects 200": (r) => r.status === 200 }) ? 1 : 0);

  sleep(Math.random() * 2 + 1);

  // Page 3: List issues
  const issuesRes = http.get(
    `${PROXY_URL}/api/0/projects/${orgSlug}/test-project/issues/`,
    { tags: { name: "web_issues" } },
  );
  pageLatency.add(issuesRes.timings.duration);
  pageSuccess.add(check(issuesRes, { "issues 200": (r) => r.status === 200 }) ? 1 : 0);

  sleep(Math.random() * 3 + 2);  // think time: 2–5s
}
