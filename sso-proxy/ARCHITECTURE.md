# SSO Proxy — Architecture & Login Flow

## Overview

The SSO proxy sits between the public internet and GlitchTip.  Every browser
request goes through it.  It owns authentication: users never see a GlitchTip
password prompt.  Instead they authenticate with Keycloak (OIDC), and the proxy
transparently provisions their GlitchTip account and forwards requests using a
GlitchTip API token.

```
Browser  ──►  SSO Proxy (:8090)  ──►  GlitchTip / Django (:8000)
                   │                        │
                   ▼                        │
              Keycloak (:8180)              │
                   │                        │
                   └──►  Redis (sessions, OIDC state)
```

---

## Components

| Component | Role |
|-----------|------|
| **sso-proxy** | FastAPI app — auth gate + reverse proxy |
| **Keycloak** | OIDC identity provider.  Holds user identities and group memberships |
| **GlitchTip** | Error tracking app (Django + Angular SPA).  Never directly exposed |
| **Redis (Valkey)** | Stores proxy sessions and OIDC state tokens.  Shared across all proxy pods |
| **PostgreSQL** | GlitchTip's database (proxy never touches it directly) |

---

## Full Login Flow

### 1. Unauthenticated request

```
Browser  GET /backend-team/issues
         ──►  Proxy: no sso_session cookie
              Store return_path="/backend-team/issues" in Redis as sso:state:{state}
              307 Redirect  ──►  Keycloak /auth?state={state}&...
```

The `state` token is a CSRF protection mechanism.  It ties the Keycloak
callback back to the original request.  It expires after 10 minutes (STATE_TTL).

### 2. Keycloak authentication

The browser shows the Keycloak login page.  If the user's Keycloak SSO session
is still valid (cookie at Keycloak's origin) this step is invisible — Keycloak
redirects back immediately without showing a form.

```
Keycloak  302 Redirect  ──►  Proxy /sso-callback?code=…&state=…
```

### 3. SSO callback (server-side)

`sso_callback` in `routes/auth.py`:

1. **Validate state** — `GETDEL sso:state:{state}` from Redis.  If missing or
   expired → 400.  This is atomic: no two pods can process the same state twice.

2. **Exchange code for tokens** — POST to Keycloak's token endpoint.

3. **Decode JWT** — Extract `email` and `groups` from the access token claims.
   (Keycloak TLS is trusted; we don't re-verify the JWT signature locally.)

4. **Provision GlitchTip account** (`glitchtip.py`):
   - Sign up or log in with a deterministic shadow password
     `HMAC-SHA256(SESSION_SECRET, email)`.  This password is never exposed to
     the user — it is only used by the proxy to obtain a Django session + API
     token on their behalf.
   - Reuse the existing `sso-proxy` API token if one already exists.

5. **Sync orgs** — For each SSO group, ensure a GlitchTip organization with the
   matching slug exists.  Invite the user as admin if not already a member.

6. **Create proxy session** — Store in Redis as `sso:session:{session_id}`:

   ```json
   {
     "email":    "alice@example.com",
     "token":    "<glitchtip-api-token>",
     "user_id":  "2",
     "id_token": "<keycloak-id-token>"
   }
   ```

   TTL = SESSION_TTL (default 8 hours).

7. **Return an HTML bridge page** — *Not* a plain 302 redirect.  See below.

### 4. The localStorage bridge (why it exists)

GlitchTip's Angular auth guard reads `localStorage.isAuthenticated`
**synchronously** at startup — before any async `/_allauth/session` call
completes.  The relevant code in the compiled bundle:

```javascript
// Auth service constructor
var j = localStorage.getItem("isAuthenticated");
this.isAuthenticated = signal(j === "true");   // synchronous read

this.loggedInGuard = computed(() => {
    let authenticated = this.isAuthenticated();
    let initialized   = this.initialized();     // false until /_allauth/session returns
    return (authenticated || initialized) ? authenticated : false;
});
```

`loggedInGuard` returns `false` until either:
- `isAuthenticated` is already `true` (from localStorage), **or**
- `initialized` becomes `true` (after the async session check)

The route guard fires *synchronously* during Angular's bootstrap.  If
`localStorage.isAuthenticated` is `"false"` (cleared along with cookies), the
guard redirects to `/login` before the session check even starts.

**The fix:** instead of `302 → return_path`, the SSO callback returns a small
HTML page that:

```html
<script>
  localStorage.setItem("isAuthenticated", "true");
  window.location.replace("/backend-team/issues/");
</script>
```

By the time the Angular SPA boots at `return_path`, localStorage already has
the correct value.  The guard passes on the first render — no login flash.

### 5. Authenticated session

Subsequent requests carry the `sso_session` cookie.  The proxy:

1. Looks up `sso:session:{session_id}` in Redis.
2. Injects `Authorization: Bearer <token>` into the forwarded request.
3. Passes the response back to the browser unchanged (modulo hop-by-hop headers).

### 6. `/_allauth/browser/v1/auth/session` (intercepted)

GlitchTip's SPA calls this endpoint periodically to verify the session.  The
proxy intercepts it and answers from Redis rather than forwarding to Django
because:

- The Django session and the proxy session are created in the same SSO callback,
  but the browser's first call can race the Django session becoming consistent.
- With multiple proxy pods the Django session cookie (tied to one pod's backend)
  may not be set on every pod; the Redis proxy session is always shared.

**Authenticated response (HTTP 200):**

```json
{
  "status": 200,
  "meta": { "is_authenticated": true },
  "data": {
    "user": { "id": 2, "email": "alice@example.com", ... },
    "methods": [{ "method": "password", "at": 1749123456.0, "email": "..." }]
  }
}
```

**Unauthenticated response (HTTP 401):**

```json
{
  "status": 401,
  "meta": { "is_authenticated": false },
  "data": { "flows": [{ "id": "login" }, { "id": "signup" }] }
}
```

HTTP 401 is what the real allauth endpoint returns.  The SPA checks
`meta.is_authenticated`, not the HTTP status, to decide what to show.

When returning 401 the proxy also sets `sso_return_hint` (5-minute cookie) to
the referer path so a re-authentication can restore the original page.

### 7. `/login?next=<path>` with a valid session (intercepted)

GlitchTip's Angular router occasionally redirects to `/login?next=<path>` even
when the user is already authenticated (the guard fired before the async session
check completed).  When the proxy sees a GET to `/login?next=…` *and* the user
has a valid proxy session, it returns `302 → next` directly, skipping the login
page entirely.

### 8. Logout

The SPA sends `DELETE /_allauth/browser/v1/auth/session`.  The proxy:

1. Deletes `sso:session:{session_id}` from Redis.
2. Clears `sso_session`, `sessionid`, and `csrftoken` cookies.
3. Stashes a `sso_pending_logout` cookie containing the Keycloak end-session URL.
4. Returns `{"status": 200, "data": {"user": null}}` (what the SPA expects).

On the next browser navigation the proxy detects `sso_pending_logout`, clears
it, and redirects the browser to Keycloak's end-session endpoint, which
terminates the SSO session and redirects back to the proxy root.

---

## Multi-Pod Deployment

The proxy is **stateless between requests**.  All shared state lives in Redis:

| Redis key | Content | TTL |
|-----------|---------|-----|
| `sso:state:{state}` | return_path for OIDC callback | 10 min |
| `sso:session:{id}` | email, API token, user_id, id_token | 8 hours |

No in-memory state survives across requests.  You can run any number of proxy
pods behind a load balancer as long as they share the same Redis instance.  The
OIDC state `GETDEL` is atomic — two pods cannot process the same callback.

**Required shared infrastructure:**
- Redis (Valkey) — already in `docker-compose.yml` as the `valkey` service
- No sticky sessions needed on the load balancer

---

## SSO-Exempt Paths

Some paths bypass SSO entirely:

| Pattern | Reason |
|---------|--------|
| `api/{id}/(store\|envelope\|...)` | Sentry SDK ingestion — authenticated via DSN in URL |
| `admin/…` | Django admin — authenticated via superuser session cookie |

---

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `KEYCLOAK_URL` | ✓ | Internal Keycloak URL (pod-to-pod) |
| `KEYCLOAK_EXTERNAL_URL` | | Browser-facing Keycloak URL (defaults to KEYCLOAK_URL) |
| `KEYCLOAK_REALM` | ✓ | Keycloak realm name |
| `KEYCLOAK_CLIENT_ID` | ✓ | OIDC client ID |
| `KEYCLOAK_CLIENT_SECRET` | ✓ | OIDC client secret |
| `GLITCHTIP_URL` | ✓ | Internal GlitchTip URL (pod-to-pod) |
| `GLITCHTIP_PROXY_TOKEN` | ✓ | GlitchTip admin API token (all scopes) |
| `PROXY_BASE_URL` | ✓ | Public URL of the proxy (used as OIDC redirect URI) |
| `SESSION_SECRET` | ✓ | Secret for deterministic shadow passwords — never change in prod |
| `REDIS_URL` | ✓ | Redis connection URL |
| `DJANGO_SUPERUSER_EMAIL` | ✓ | GlitchTip superuser (for admin API calls) |
| `DJANGO_SUPERUSER_PASSWORD` | ✓ | GlitchTip superuser password |

`SESSION_SECRET`, `GLITCHTIP_PROXY_TOKEN`, and `DJANGO_SUPERUSER_PASSWORD` are
validated at startup — the process will refuse to start if they contain the
default placeholder values.

---

## Redis Key Layout

```
sso:state:{16-byte-urlsafe-token}   →  "/backend-team/issues/"   (STRING, TTL 600s)
sso:session:{32-byte-urlsafe-token} →  JSON object               (STRING, TTL 28800s)
```

---

## Session Lifetime

| Timer | Default | Controlled by |
|-------|---------|---------------|
| OIDC state | 10 min | `STATE_TTL` in `config.py` |
| Proxy session | 8 hours | `SESSION_TTL` in `config.py` |
| Keycloak SSO session | per-realm policy | Keycloak admin console |
| `sso_return_hint` cookie | 5 min | hardcoded in `allauth_session_get` |
| `sso_pending_logout` cookie | 60 s | hardcoded in `logout` |

When a proxy session expires the next request is redirected to Keycloak.  If the
Keycloak SSO session is still alive the user is re-authenticated transparently
(no login form shown).
