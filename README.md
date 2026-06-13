# GlitchTip + Keycloak SSO Group Sync

GlitchTip deployment with automatic SSO group → organization mapping.

GlitchTip does not support OIDC/SAML group sync natively. This setup adds a lightweight reverse proxy (`sso-proxy`) in front of GlitchTip that handles all OIDC authentication with Keycloak, extracts group membership from the JWT, creates GlitchTip organizations per group, and adds the user as an admin member — automatically on every login.

## Architecture

```
Browser
  │
  ▼
sso-proxy :8090          ← public entry point
  │  handles OIDC with Keycloak
  │  syncs SSO groups → GlitchTip orgs
  │  proxies all traffic with Bearer token
  │
  ├──► Keycloak :8180    ← SSO provider (OIDC)
  │
  └──► GlitchTip :8000  ← internal only, not exposed to host
         │
         └──► Postgres   ← shared DB
```

**Sentry SDK traffic** (DSN-authenticated) bypasses the SSO check and goes straight to GlitchTip.

## Services

| Service | Port | Description |
|---|---|---|
| `sso-proxy` | 8090 | Public entry point — handles OIDC + proxies to GlitchTip |
| `keycloak` | 8180 | SSO provider — admin UI at http://localhost:8180 |
| `web` | internal | GlitchTip application (not exposed to host) |
| `postgres` | internal | Database |
| `glitchtip-init` | — | One-shot bootstrap: creates superuser + admin API token |

## Quick Start

### 1. Prerequisites

- Docker + Docker Compose
- Ports 8090 and 8180 free on your machine

### 2. Start the stack

```bash
docker compose up -d
```

Wait ~3 minutes for Keycloak's first-boot database migrations to complete.

### 3. Bootstrap the admin token

The `glitchtip-init` service creates a superuser and the proxy admin API token. If it fails to run automatically (known issue with the init container), run it manually:

```bash
docker compose up glitchtip-init
```

Or directly inside the GlitchTip container:

```bash
docker cp glitchtip-init.py glitchtip-web-1:/glitchtip-init.py
docker exec glitchtip-web-1 python /glitchtip-init.py
```

### 4. Open the app

Navigate to **http://localhost:8090** in Chrome (Firefox also works; Safari may have connection issues on first load).

You will be redirected to Keycloak for login.

## Test Users

Defined in `keycloak/realm.json`:

| Email | Password | Groups |
|---|---|---|
| `bar@example.com` | `password` | `platform-team` |
| `alice@example.com` | `password` | `platform-team`, `backend-team` |

After first login, each user will see GlitchTip organizations matching their Keycloak groups.

## SSO Login Flow

1. Browser hits `sso-proxy` → no session → redirect to Keycloak
2. User authenticates with Keycloak (once)
3. Keycloak redirects to `/sso-callback?code=...`
4. Proxy exchanges code for JWT, extracts `groups` claim
5. Proxy creates a GlitchTip account for the user (idempotent)
6. Proxy creates GlitchTip orgs for each group (idempotent) and adds the user as admin
7. Proxy sets session cookies and redirects to GlitchTip dashboard

Subsequent logins reuse the existing account and token; orgs are synced on every login.

## Configuration

### Environment variables (docker-compose.yml)

| Variable | Service | Description |
|---|---|---|
| `SECRET_KEY` | web | Django secret key — change in production |
| `GLITCHTIP_PROXY_TOKEN` | web, glitchtip-init, sso-proxy | Shared admin API token — change in production |
| `SESSION_SECRET` | sso-proxy | Used to derive per-user GlitchTip passwords — change in production |
| `KEYCLOAK_CLIENT_SECRET` | sso-proxy | Keycloak client secret |
| `GLITCHTIP_DOMAIN` | web | Public URL of the proxy (must match `PROXY_BASE_URL`) |
| `PROXY_BASE_URL` | sso-proxy | Public URL of the proxy |

> **Production note:** Change `SECRET_KEY`, `GLITCHTIP_PROXY_TOKEN`, and `SESSION_SECRET` before deploying. Use `openssl rand -hex 32` to generate each one.

### Adding Keycloak groups

Edit `keycloak/realm.json` to add groups and assign users before first boot. After Keycloak is running, use the Keycloak Admin UI at http://localhost:8180 (admin / admin):

- **Realm:** `glitchtip`
- Create groups under **Groups**
- Assign users under **Users → Groups tab**

New groups are automatically created as GlitchTip orgs on the user's next login.

### Adding users to Keycloak

Via Admin UI: **Users → Add user** → set email, assign to groups, set a password under the **Credentials** tab.

## Sentry SDK / DSN Integration

SDK traffic bypasses SSO. Use DSN URLs pointing at the proxy:

```
http://localhost:8090/api/<project-id>/store/
```

Requests with an `X-Sentry-Auth` header, or paths matching `api/<id>/(store|envelope|minidump|...)`, are forwarded directly to GlitchTip without session checks.

## Troubleshooting

### "Invalid redirect_uri" on Keycloak login

Keycloak's database has a stale redirect URI from a previous run. Fix:

```bash
docker compose stop keycloak && docker compose rm -f keycloak && docker compose up -d keycloak
```

Wait ~3 minutes for Keycloak to re-import the realm.

Alternatively, update it in the Admin UI: **Clients → sso-proxy → Settings → Valid redirect URIs**.

### Slow first load

Keycloak runs in `start-dev` mode with an embedded database. The JVM takes 2–4 minutes to warm up on first boot. Subsequent requests are faster.

### Organizations not showing after login

The first login creates a pending org membership invite. If orgs still don't appear after logging in, run:

```bash
docker exec glitchtip-web-1 python3 -c "
import sys, os
sys.path.insert(0, '/code')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'glitchtip.settings')
import django; django.setup()
from apps.organizations_ext.models import OrganizationUser, Organization
from django.contrib.auth import get_user_model
User = get_user_model()
email = 'YOUR_EMAIL_HERE'
user = User.objects.get(email=email)
for ou in OrganizationUser.objects.filter(email=email, user=None):
    ou.user = user; ou.save()
    print('Fixed:', ou.organization.slug)
"
```

### Logout redirects to "invalid redirect URI"

This only happens if Keycloak was already running before `postLogoutRedirectUris` was added to `realm.json`. For a fresh deploy this is handled automatically. To fix a running instance, either recreate the Keycloak container:

```bash
docker compose stop keycloak && docker compose rm -f keycloak && docker compose up -d keycloak
```

Or add it via the Admin UI: **Clients → sso-proxy → Settings → Valid post logout redirect URIs → add `http://localhost:8090/`**.

## Testing

### Automated test suite (pytest + Playwright)

```bash
cd tests
pip install -r requirements.txt
playwright install chromium

# Set required env vars (these match docker-compose defaults):
export GLITCHTIP_PROXY_TOKEN=changeme
export KEYCLOAK_ADMIN_PASSWORD=admin

pytest -v
```

13 tests across two files:

| File | Coverage |
|---|---|
| `test_auth.py` | SSO login flow, org redirect, localStorage bridge, session persistence, logout |
| `test_dsn.py` | DSN/envelope bypass (no session required), event storage and retrieval |

Tests run against `http://localhost:8090` by default. Override for other environments:

```bash
PROXY_URL=http://my-cluster GLITCHTIP_URL=http://my-glitchtip pytest -v
```

---

## Kubernetes / Helm

### Prerequisites

```bash
brew install kind helm k6
```

### Local dev — kind cluster

One script provisions a kind cluster, builds and loads the Docker image, deploys the Helm umbrella chart, then runs the pytest suite against it:

```bash
bash scripts/kind-deploy.sh
```

The script:
1. Creates a 3-node kind cluster (`helm/kind-cluster.yaml`) with ports 80→8888 and 443→8889
2. Installs nginx-ingress (pinned to control-plane node)
3. Builds `glitchtip-sso-proxy:dev` (arm64) and loads it into the cluster
4. Runs `helm dep update` + `helm upgrade --install` with `values-kind.yaml` overrides
5. Port-forwards GlitchTip to `:8003` and Valkey to `:6381`, then runs `pytest tests/ -v` against the nginx ingress on `:8888`

> **Note:** Keycloak is not deployed in kind — the script reuses the docker-compose Keycloak running on the host at `http://localhost:8180`. Run `docker compose up -d keycloak` before deploying.

### Production — OpenShift

The umbrella chart ships a separate values file for production deployments where PostgreSQL, Redis, and Red Hat SSO are managed externally:

```bash
helm upgrade --install glitchtip helm/glitchtip-umbrella \
  -f helm/glitchtip-umbrella/values-production.yaml \
  --set sso-proxy.config.KEYCLOAK_URL=https://sso.apps.cluster.company.com \
  --set sso-proxy.config.GLITCHTIP_URL=http://glitchtip-web:8000 \
  --set sso-proxy.config.PROXY_BASE_URL=https://glitchtip.apps.cluster.company.com \
  --set sso-proxy.config.REDIS_URL=redis://external-redis:6379 \
  --set sso-proxy.secrets.SESSION_SECRET="$(openssl rand -hex 32)" \
  --set sso-proxy.secrets.KEYCLOAK_CLIENT_SECRET="your-client-secret" \
  --set sso-proxy.secrets.GLITCHTIP_PROXY_TOKEN="your-admin-token"
```

Key differences from dev:
- `postgresql`, `valkey`, `keycloak` sub-charts disabled
- `openshift.route.enabled: true` — creates an OpenShift Route instead of Ingress
- SSO proxy runs as `USER 1000` (numeric UID required by OpenShift SCCs)
- `readOnlyRootFilesystem: true`, all capabilities dropped

### Helm chart structure

```
helm/
├── kind-cluster.yaml              # kind cluster config (1 control-plane + 2 workers)
├── sso-proxy/                     # Standalone sso-proxy chart
│   ├── Chart.yaml
│   ├── values.yaml
│   └── templates/
│       ├── deployment.yaml        # envFrom ConfigMap + Secret; readOnlyRootFilesystem
│       ├── service.yaml
│       ├── configmap.yaml
│       ├── secret.yaml
│       ├── ingress.yaml
│       ├── route.yaml             # OpenShift Route (gated by openshift.route.enabled)
│       └── hpa.yaml               # autoscaling/v2, 2–10 replicas at 70% CPU
└── glitchtip-umbrella/            # Umbrella chart
    ├── Chart.yaml                 # deps: glitchtip, sso-proxy, postgresql, valkey, keycloak
    ├── values.yaml                # Dev defaults (all services enabled)
    ├── values-kind.yaml           # imagePullPolicy: Never, localhost ingress
    ├── values-production.yaml     # OpenShift: external services, Route enabled
    └── templates/
        ├── glitchtip-init-job.yaml        # post-install/post-upgrade Job (replaces init container)
        └── keycloak-realm-configmap.yaml  # Mounts realm.json into Keycloak pod
```

---

## Load Testing

Requires k6 (`brew install k6`). Tests are in `load-tests/`.

```bash
# Run all three suites and save JSON results to load-tests/results/<timestamp>/
bash load-tests/run.sh all

# Run individual suites
bash load-tests/run.sh dsn   # DSN ingestion only
bash load-tests/run.sh web   # Dashboard browsing only
bash load-tests/run.sh full  # Combined realistic scenario
```

| Suite | Scenario | Target |
|---|---|---|
| `k6-dsn.js` | 100 simulated websites sending error events | Ramp to 500 RPS, p95 < 500ms, < 1% errors |
| `k6-web.js` | 200 concurrent authenticated dashboard users | p95 < 2s, < 2% errors |
| `k6-full.js` | Combined 80% DSN + 20% web (realistic production mix) | Both thresholds together |

The load test is sized for **100 websites × 500 average users** (50,000 users total). The 500 RPS DSN target is 35× the steady-state production rate, confirming headroom for traffic spikes.

Override defaults via env vars:
```bash
PROXY_URL=http://my-cluster DSN_KEY=abc123 bash load-tests/run.sh dsn
```

---

## Architecture Reference

See [sso-proxy/ARCHITECTURE.md](sso-proxy/ARCHITECTURE.md) for a full description of:
- OIDC authorization code flow
- localStorage bridge (why it exists and how it works)
- Multi-pod stateless design with Redis key layout
- Session lifetimes and all environment variables

---

## File Structure

```
.
├── docker-compose.yml             # Local dev stack
├── glitchtip-init.py              # Bootstrap: superuser + admin API token
├── keycloak/
│   └── realm.json                 # Keycloak realm, users, groups, client config
├── sso-proxy/
│   ├── Dockerfile                 # Non-root USER 1000 (OpenShift SCC compatible)
│   ├── requirements.txt
│   ├── main.py                    # FastAPI app entry point + /healthz
│   ├── config.py                  # Env var config
│   ├── store.py                   # Redis client singleton
│   ├── glitchtip.py               # GlitchTip API helpers (create user/org/member)
│   ├── routes/
│   │   ├── auth.py                # /sso-login, /sso-callback, /sso-logout
│   │   └── proxy.py               # /{path:path} catch-all reverse proxy
│   └── ARCHITECTURE.md            # Detailed design doc
├── tests/
│   ├── conftest.py                # Fixtures: browser, base URLs, API helpers
│   ├── test_auth.py               # SSO flow browser tests (Playwright)
│   └── test_dsn.py                # DSN bypass + event ingestion tests
├── helm/
│   ├── kind-cluster.yaml
│   ├── sso-proxy/                 # sso-proxy Helm chart
│   └── glitchtip-umbrella/        # Umbrella chart
├── scripts/
│   └── kind-deploy.sh             # End-to-end: create cluster → deploy → test
├── load-tests/
│   ├── k6-dsn.js
│   ├── k6-web.js
│   ├── k6-full.js
│   └── run.sh
└── test-app/
    └── app.py                     # Manual demo app for rich-event SDK testing
```
