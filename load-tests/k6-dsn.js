/**
 * k6-dsn.js — DSN event ingestion load test
 *
 * Simulates 100 websites sending Sentry SDK events through the SSO proxy.
 * The proxy must forward these to GlitchTip without requiring SSO authentication
 * (SDK_PATH_RE exemption in proxy.py).
 *
 * Capacity target:
 *   100 websites × 500 avg users × 1 error/2h = ~7 events/s steady state
 *   10× spike: 70 events/s  →  test target: 500 RPS to confirm headroom
 *
 * Run:
 *   k6 run load-tests/k6-dsn.js
 *   k6 run --out json=results/dsn-results.json load-tests/k6-dsn.js
 *
 * Environment variables:
 *   PROXY_URL   — default http://localhost:8090
 *   PROJECT_ID  — GlitchTip project ID (default 1)
 *   DSN_KEY     — DSN public key (default from test-automation project)
 */
import http from "k6/http";
import { check, sleep } from "k6";
import { Counter, Rate, Trend } from "k6/metrics";

// ── Config ────────────────────────────────────────────────────────────────────

const PROXY_URL  = __ENV.PROXY_URL  || "http://localhost:8090";
const PROJECT_ID = __ENV.PROJECT_ID || "1";
const DSN_KEY    = __ENV.DSN_KEY    || "5d17c8a90895453f87e42b14d768f10c";

// ── Custom metrics ────────────────────────────────────────────────────────────

const eventsSent    = new Counter("dsn_events_sent");
const eventsAccepted = new Rate("dsn_events_accepted");
const storeLatency  = new Trend("dsn_store_latency_ms", true);

// ── Test options ──────────────────────────────────────────────────────────────

export const options = {
  scenarios: {
    // Ramp up to 100 RPS, hold, then spike to 500 RPS
    steady_load: {
      executor: "ramping-arrival-rate",
      startRate: 10,
      timeUnit: "1s",
      preAllocatedVUs: 50,
      maxVUs: 300,
      stages: [
        { duration: "30s", target: 50  },   // warm up
        { duration: "60s", target: 100 },   // steady (7× production peak)
        { duration: "30s", target: 500 },   // spike (35× production peak)
        { duration: "60s", target: 500 },   // sustain spike
        { duration: "30s", target: 0   },   // cool down
      ],
    },
  },
  thresholds: {
    http_req_duration:   ["p(95)<500"],   // 95th percentile under 500ms
    http_req_failed:     ["rate<0.01"],   // <1% HTTP errors
    dsn_events_accepted: ["rate>0.99"],   // >99% events accepted by GlitchTip
  },
};

// ── Helpers ───────────────────────────────────────────────────────────────────

function uuidv4() {
  return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0;
    return (c === "x" ? r : (r & 0x3) | 0x8).toString(16);
  });
}

// Simulate events from 100 different websites (projects) using the same DSN
// but varying the transaction/component metadata.
const WEBSITES = Array.from({ length: 100 }, (_, i) => `website-${i + 1}.example.com`);
const ERRORS   = [
  { type: "TypeError",           value: "Cannot read properties of undefined (reading 'id')" },
  { type: "ValueError",          value: "invalid literal for int() with base 10" },
  { type: "KeyError",            value: "'user_id' not found in session" },
  { type: "ConnectionError",     value: "Failed to establish a new connection to database" },
  { type: "TimeoutError",        value: "Request to downstream service timed out after 30s" },
  { type: "PermissionDenied",    value: "User does not have permission to perform this action" },
  { type: "AttributeError",      value: "NoneType object has no attribute 'email'" },
  { type: "RuntimeError",        value: "Unhandled state in payment processing pipeline" },
];

function buildEvent() {
  const website = WEBSITES[Math.floor(Math.random() * WEBSITES.length)];
  const error   = ERRORS[Math.floor(Math.random() * ERRORS.length)];
  return JSON.stringify({
    event_id:    uuidv4().replace(/-/g, ""),
    timestamp:   new Date().toISOString(),
    platform:    "python",
    level:       "error",
    environment: "production",
    server_name: website,
    transaction: `/api/v1/${Math.random() > 0.5 ? "users" : "orders"}`,
    release:     "1.0." + Math.floor(Math.random() * 100),
    tags:        { site: website, region: "eu-west-1" },
    user:        { id: String(Math.floor(Math.random() * 50000)) },
    exception: {
      values: [{
        type:       error.type,
        value:      error.value,
        stacktrace: {
          frames: [
            { filename: "app/views.py", lineno: 42, function: "handle_request" },
            { filename: "app/models.py", lineno: 18, function: "get_user" },
          ],
        },
      }],
    },
  });
}

// ── Main scenario ─────────────────────────────────────────────────────────────

export default function () {
  const payload = buildEvent();
  const ts      = Math.floor(Date.now() / 1000);
  const headers = {
    "Content-Type":  "application/json",
    "X-Sentry-Auth": `Sentry sentry_version=7, sentry_client=k6-load-test/1.0, sentry_timestamp=${ts}, sentry_key=${DSN_KEY}`,
  };

  const res = http.post(`${PROXY_URL}/api/${PROJECT_ID}/store/`, payload, { headers });

  storeLatency.add(res.timings.duration);
  eventsSent.add(1);

  const ok = check(res, {
    "status is 200":             (r) => r.status === 200,
    "not redirected to Keycloak": (r) => r.status !== 307 && r.status !== 302,
  });
  eventsAccepted.add(ok ? 1 : 0);
}
