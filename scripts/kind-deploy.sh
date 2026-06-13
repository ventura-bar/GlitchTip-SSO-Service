#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# kind-deploy.sh — Build, deploy, and test GlitchTip + SSO proxy on a local
#                  kind Kubernetes cluster.
#
# Prerequisites: kind, kubectl, helm, docker
#
# Usage:
#   bash scripts/kind-deploy.sh [--skip-cluster] [--skip-tests]
#     --skip-cluster   Reuse an existing 'glitchtip' kind cluster
#     --skip-tests     Deploy only, do not run pytest
#
# Notes:
#   - Keycloak is NOT deployed in kind — this script reuses the docker-compose
#     Keycloak running on the host at http://localhost:8180. Start it first:
#       docker compose up -d keycloak
#   - The nginx ingress maps to host port 8888 (set in helm/kind-cluster.yaml).
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

CLUSTER_NAME="glitchtip"
NAMESPACE="glitchtip"
CHART_DIR="helm/glitchtip-umbrella"
SKIP_CLUSTER=false
SKIP_TESTS=false

for arg in "$@"; do
  case $arg in
    --skip-cluster) SKIP_CLUSTER=true ;;
    --skip-tests)   SKIP_TESTS=true ;;
  esac
done

# ── 1. kind cluster ───────────────────────────────────────────────────────────
if [ "$SKIP_CLUSTER" = false ]; then
  echo "▶ Creating kind cluster '$CLUSTER_NAME'..."
  if kind get clusters 2>/dev/null | grep -q "^${CLUSTER_NAME}$"; then
    echo "  Cluster already exists — deleting and recreating."
    kind delete cluster --name "$CLUSTER_NAME"
  fi
  kind create cluster --config helm/kind-cluster.yaml --name "$CLUSTER_NAME"

  echo "▶ Installing nginx ingress controller..."
  kubectl apply -f https://raw.githubusercontent.com/kubernetes/ingress-nginx/main/deploy/static/provider/kind/deploy.yaml
  # Pin the ingress controller to the control-plane node (the only port-mapped node)
  kubectl patch deployment ingress-nginx-controller -n ingress-nginx \
    --type='json' \
    -p='[{"op":"add","path":"/spec/template/spec/nodeSelector","value":{"ingress-ready":"true"}}]'
  kubectl wait -n ingress-nginx \
    --for=condition=ready pod \
    --selector=app.kubernetes.io/component=controller \
    --timeout=120s
fi

kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -

# ── 2. Build and load images ──────────────────────────────────────────────────
echo "▶ Building sso-proxy image (arm64)..."
docker buildx build --platform linux/arm64 \
  --output "type=docker,name=glitchtip-sso-proxy:dev" \
  ./sso-proxy

echo "▶ Loading images into kind..."
kind load docker-image glitchtip-sso-proxy:dev --name "$CLUSTER_NAME"
if ! docker image inspect glitchtip/glitchtip:6 &>/dev/null; then
  docker pull --platform linux/arm64 glitchtip/glitchtip:6
fi
kind load docker-image glitchtip/glitchtip:6 --name "$CLUSTER_NAME"

# ── 3. Helm dependency update ─────────────────────────────────────────────────
echo "▶ Updating Helm dependencies..."
helm repo add glitchtip https://gitlab.com/api/v4/projects/16325141/packages/helm/stable 2>/dev/null || true
helm repo add bitnami https://charts.bitnami.com/bitnami 2>/dev/null || true
helm repo update
helm dep update "$CHART_DIR"

# ── 4. Deploy umbrella chart ──────────────────────────────────────────────────
SESSION_SECRET="$(openssl rand -hex 32)"
DJANGO_PASS="adminpass123"
PG_PASS="pgpass123"
GLITCHTIP_SECRET="$(openssl rand -hex 32)"
PROXY_TOKEN="$(openssl rand -hex 20)"

echo "▶ Deploying umbrella chart..."
# Delete any stale migration jobs (Helm pre-upgrade hook will fail if they exist)
kubectl delete jobs -n "$NAMESPACE" --all --ignore-not-found 2>/dev/null || true

helm upgrade --install glitchtip "$CHART_DIR" \
  --namespace "$NAMESPACE" \
  -f "$CHART_DIR/values.yaml" \
  -f "$CHART_DIR/values-kind.yaml" \
  --set "sso-proxy.secrets.SESSION_SECRET=${SESSION_SECRET}" \
  --set "sso-proxy.secrets.DJANGO_SUPERUSER_PASSWORD=${DJANGO_PASS}" \
  --set "sso-proxy.secrets.KEYCLOAK_CLIENT_SECRET=sso-proxy-secret" \
  --set "sso-proxy.secrets.GLITCHTIP_PROXY_TOKEN=${PROXY_TOKEN}" \
  --set "postgresql.auth.password=${PG_PASS}" \
  --set "glitchtip.glitchtip.secretKey=${GLITCHTIP_SECRET}" \
  --set "glitchtip.glitchtip.database.existingSecret=glitchtip-db-creds" \
  --set "glitchtip.glitchtip.valkey.existingSecret=glitchtip-db-creds" \
  --wait --timeout 8m

echo "▶ Waiting for all pods to be ready..."
kubectl wait --namespace "$NAMESPACE" \
  --for=condition=ready pod --all --timeout=300s

kubectl get pods -n "$NAMESPACE"

# ── 5. Run tests ──────────────────────────────────────────────────────────────
if [ "$SKIP_TESTS" = false ]; then
  echo "▶ Starting port-forwards for tests..."
  # Direct GlitchTip access (bypasses sso-proxy for admin API calls in fixtures)
  kubectl port-forward -n "$NAMESPACE" svc/glitchtip-web 8003:80 &
  PF_GT=$!
  # Valkey for Redis session inspection
  kubectl port-forward -n "$NAMESPACE" svc/glitchtip-valkey-primary 6381:6379 &
  PF_REDIS=$!

  sleep 3   # Give port-forwards time to establish

  echo "▶ Running test suite against kind deployment..."
  echo "   Proxy:      http://localhost:8888  (nginx ingress)"
  echo "   GlitchTip:  http://localhost:8003  (port-forward)"
  echo "   Keycloak:   http://localhost:8180  (docker-compose host)"
  echo "   Valkey:     redis://localhost:6381 (port-forward)"

  PROXY_URL=http://localhost:8888 \
  GLITCHTIP_URL=http://localhost:8003 \
  REDIS_URL=redis://localhost:6381 \
  KEYCLOAK_URL=http://localhost:8180 \
  GLITCHTIP_PROXY_TOKEN="${PROXY_TOKEN}" \
  KEYCLOAK_ADMIN_PASSWORD="keycloak_admin_local" \
    python -m pytest tests/ -v --tb=short || true

  kill "$PF_GT" "$PF_REDIS" 2>/dev/null || true
fi

echo ""
echo "✓ Deployment complete."
echo "  SSO Proxy:  http://localhost:8888  (nginx ingress)"
echo "  Keycloak:   http://localhost:8180  (docker-compose, external)"
echo "  Token:      ${PROXY_TOKEN}"
echo "  To delete:  kind delete cluster --name $CLUSTER_NAME"
