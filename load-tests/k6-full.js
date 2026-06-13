/**
 * k6-full.js — Realistic combined load test
 *
 * Weighted mix matching production traffic patterns for 100 websites with
 * medium traffic (100-1000 users each, average 500):
 *
 *   80% — DSN event ingestion (SDK sending errors from websites)
 *   20% — Authenticated dashboard browsing (team members checking GlitchTip)
 *
 * At 100 concurrent VUs this generates approximately:
 *   ~80 RPS  DSN events   (vs ~14 RPS steady-state production → 6× headroom)
 *   ~20 RPS  dashboard    (vs ~8 RPS steady-state production → 2.5× headroom)
 *
 * Run:
 *   k6 run load-tests/k6-full.js
 *   k6 run --out json=results/full-results.json load-tests/k6-full.js
 */
import http from "k6/http";
import { check, sleep } from "k6";
import { Counter, Rate, Trend } from "k6/metrics";

// ── Config ────────────────────────────────────────────────────────────────────

const PROXY_URL        = __ENV.PROXY_URL        || "http://localhost:8090";
const PROJECT_ID       = __ENV.PROJECT_ID       || "1";
const DSN_KEY          = __ENV.DSN_KEY          || "5d17c8a90895453f87e42b14d768f10c";
const KEYCLOAK_URL     = __ENV.KEYCLOAK_URL     || "http://localhost:8180";
const KC_REALM         = __ENV.KC_REALM         || "glitchtip";
const KC_CLIENT_ID     = __ENV.KC_CLIENT_ID     || "sso-proxy";
const KC_CLIENT_SECRET = __ENV.KC_CLIENT_SECRET || "sso-proxy-secret";

// ── Metrics ───────────────────────────────────────────────────────────────────

const dsnLatency  = new Trend("dsn_latency_ms", true);
const webLatency  = new Trend("web_latency_ms", true);
const dsnRate     = new Rate("dsn_accepted");
const webRate     = new Rate("web_success");
const totalEvents = new Counter("total_events_ingested");

// ── Test options ──────────────────────────────────────────────────────────────

export const options = {
  scenarios: {
    // 80% of VUs simulate SDK event ingestion
    sdk_events: {
      executor: "ramping-arrival-rate",
      startRate: 10,
      timeUnit: "1s",
      preAllocatedVUs: 50,
      maxVUs: 300,
      stages: [
        { duration: "1m",  target: 80  },
        { duration: "3m",  target: 80  },
        { duration: "1m",  target: 200 },
        { duration: "2m",  target: 200 },
        { duration: "30s", target: 0   },
      ],
      exec: "dsnScenario",
    },
    // 20% of VUs simulate dashboard browsing
    dashboard: {
      executor: "ramping-vus",
      startVUs: 0,
      stages: [
        { duration: "1m",  target: 20  },
        { duration: "3m",  target: 20  },
        { duration: "1m",  target: 50  },
        { duration: "2m",  target: 50  },
        { duration: "30s", target: 0   },
      ],
      exec: "webScenario",
    },
  },
  thresholds: {
    "http_req_duration{scenario:sdk_events}": ["p(95)<500"],
    "http_req_duration{scenario:dashboard}":  ["p(95)<2000"],
    http_req_failed:  ["rate<0.01"],
    dsn_accepted:     ["rate>0.99"],
    web_success:      ["rate>0.95"],
  },
};

// ── Shared helpers ────────────────────────────────────────────────────────────

function uuidv4() {
  return "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx".replace(/x/g, () =>
    Math.floor(Math.random() * 16).toString(16),
  );
}

const ERRORS = [
  { type: "TypeError",       value: "Cannot read properties of undefined" },
  { type: "ValueError",      value: "invalid literal for int()" },
  { type: "KeyError",        value: "session key not found" },
  { type: "ConnectionError", value: "connection refused" },
  { type: "TimeoutError",    value: "request timed out after 30s" },
];

let webToken = null;

function getWebToken() {
  if (webToken) return webToken;
  const res = http.post(
    `${KEYCLOAK_URL}/realms/${KC_REALM}/protocol/openid-connect/token`,
    {
      grant_type:    "password",
      client_id:     KC_CLIENT_ID,
      client_secret: KC_CLIENT_SECRET,
      username:      "alice",
      password:      "password",
      scope:         "openid",
    },
  );
  if (res.status === 200) {
    webToken = res.json().access_token;
  }
  return webToken;
}

// ── DSN scenario (80%) ────────────────────────────────────────────────────────

export function dsnScenario() {
  const error   = ERRORS[Math.floor(Math.random() * ERRORS.length)];
  const payload = JSON.stringify({
    event_id:  uuidv4(),
    timestamp: new Date().toISOString(),
    platform:  "python",
    level:     "error",
    server_name: `site-${Math.floor(Math.random() * 100) + 1}.example.com`,
    exception: { values: [{ type: error.type, value: error.value }] },
  });
  const ts  = Math.floor(Date.now() / 1000);
  const res = http.post(
    `${PROXY_URL}/api/${PROJECT_ID}/store/`,
    payload,
    {
      headers: {
        "Content-Type":  "application/json",
        "X-Sentry-Auth": `Sentry sentry_version=7, sentry_key=${DSN_KEY}, sentry_timestamp=${ts}`,
      },
      tags: { endpoint: "dsn_store" },
    },
  );
  dsnLatency.add(res.timings.duration);
  totalEvents.add(1);
  dsnRate.add(check(res, { "dsn 200": (r) => r.status === 200 }) ? 1 : 0);
}

// ── Web scenario (20%) ────────────────────────────────────────────────────────

export function webScenario() {
  const token = getWebToken();
  if (!token) { sleep(1); return; }

  const headers = { Authorization: `Bearer ${token}` };

  // List orgs
  const r1 = http.get(`${PROXY_URL}/api/0/organizations/`, { headers, tags: { endpoint: "web_orgs" } });
  webLatency.add(r1.timings.duration);
  webRate.add(check(r1, { "orgs 200": (r) => r.status === 200 }) ? 1 : 0);
  sleep(Math.random() * 2 + 1);

  // List issues
  const r2 = http.get(
    `${PROXY_URL}/api/0/projects/test-automation/test-project/issues/`,
    { headers, tags: { endpoint: "web_issues" } },
  );
  webLatency.add(r2.timings.duration);
  webRate.add(check(r2, { "issues 200": (r) => r.status === 200 }) ? 1 : 0);
  sleep(Math.random() * 3 + 2);
}

// Default export required by k6 even when using named scenarios
export default function () {}
