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

## File Structure

```
.
├── docker-compose.yml        # Stack definition
├── glitchtip-init.py         # Bootstrap script (superuser + admin API token)
├── keycloak/
│   └── realm.json            # Keycloak realm, users, groups, client config
└── sso-proxy/
    ├── Dockerfile
    ├── requirements.txt
    └── main.py               # FastAPI proxy: OIDC handler + org sync + reverse proxy
```
