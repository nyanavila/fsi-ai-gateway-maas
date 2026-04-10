#!/usr/bin/env bash
set -euo pipefail

NAMESPACE="fsi-ai-gateway-maas"
IMAGE="quay.io/navila/ai-gateway-maas:latest"
CLUSTER="https://api.cluster-9n5fl.9n5fl.sandbox3963.opentlc.com:6443"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info()    { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
die()     { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }
section() { echo -e "\n${GREEN}══════════════════════════════════════${NC}"; echo -e "${GREEN} $*${NC}"; echo -e "${GREEN}══════════════════════════════════════${NC}"; }

section "Preflight"
command -v oc     &>/dev/null || die "oc not found"
command -v podman &>/dev/null || die "podman not found"
[[ -z "${ANTHROPIC_API_KEY:-}" ]] && die "ANTHROPIC_API_KEY not set"
[[ -z "${MAAS_API_KEY:-}" ]]      && die "MAAS_API_KEY not set"
info "Logged in as: $(oc whoami)"
info "Cluster: $(oc whoami --show-server)"
info "Original project still running: fsi-ai-gateway"

section "Namespace"
if oc get project "$NAMESPACE" &>/dev/null; then
  info "Project $NAMESPACE already exists"
else
  oc new-project "$NAMESPACE"     --description="FSI AI Gateway v2 — Red Hat MaaS + Anthropic"     --display-name="FSI AI Gateway (MaaS)"
fi
oc project "$NAMESPACE"

section "Build & push image"
info "Building $IMAGE (this is a NEW image — original is unaffected)"
podman build -t "$IMAGE" .
info "Pushing $IMAGE"
podman push "$IMAGE"

section "Secrets"
if oc get secret ai-gateway-secrets -n "$NAMESPACE" &>/dev/null; then
  warn "Secret exists — recreating"
  oc delete secret ai-gateway-secrets -n "$NAMESPACE"
fi
oc create secret generic ai-gateway-secrets   --from-literal=ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY"   --from-literal=MAAS_API_KEY="$MAAS_API_KEY"   -n "$NAMESPACE"
info "Secrets created"

section "Apply manifests"
for f in openshift/configmap.yaml openshift/redis.yaml openshift/deployment.yaml           openshift/service-route.yaml openshift/networkpolicy.yaml; do
  info "Applying $f"
  oc apply -f "$f" -n "$NAMESPACE"
done

section "Waiting for rollout"
info "Redis..."
oc rollout status deployment/redis -n "$NAMESPACE" --timeout=3m
info "AI Gateway (MaaS)..."
oc rollout status deployment/ai-gateway -n "$NAMESPACE" --timeout=5m

section "Smoke test"
GATEWAY=$(oc get route ai-gateway -n "$NAMESPACE" -o jsonpath='{.spec.host}')
info "Gateway URL: https://$GATEWAY"
sleep 3
HTTP=$(curl -sk -o /dev/null -w "%{http_code}" "https://$GATEWAY/health")
if [[ "$HTTP" == "200" ]]; then
  info "Health check passed"
  curl -sk "https://$GATEWAY/health" | python3 -m json.tool
else
  warn "Health returned HTTP $HTTP"
fi

info "Provider status:"
curl -sk "https://$GATEWAY/v1/providers" | python3 -m json.tool

section "Done"
echo ""
echo "  NEW (MaaS):      https://$GATEWAY"
echo "  ORIGINAL (v1):   https://ai-gateway-fsi-ai-gateway.apps.cluster-9n5fl.9n5fl.sandbox3963.opentlc.com"
echo ""
echo "  Quick test:"
echo "  curl -X POST https://$GATEWAY/v1/chat \"
echo "    -H 'Content-Type: application/json' \"
echo "    -d '{\"message\": \"What are your opening hours?\", \"department\": \"CX\"}'"
echo ""

# ── Demo UI ───────────────────────────────────────────────────────────────────
section "Demo UI"
DEMO_IMAGE="quay.io/navila/gateway-demo-maas:latest"
if command -v podman &>/dev/null && [ -d "demo" ]; then
  info "Building demo image"
  podman build \
    --build-arg REACT_APP_GATEWAY_URL="https://ai-gateway-fsi-ai-gateway-maas.apps.cluster-9n5fl.9n5fl.sandbox3963.opentlc.com" \
    -f demo/Dockerfile \
    -t "$DEMO_IMAGE" \
    demo/
  podman push "$DEMO_IMAGE"
  oc apply -f demo/openshift-demo.yaml -n "$NAMESPACE"
  oc rollout status deployment/gateway-demo -n "$NAMESPACE" --timeout=3m
  DEMO=$(oc get route gateway-demo -n "$NAMESPACE" -o jsonpath='{.spec.host}')
  info "Demo URL: https://$DEMO"
else
  warn "demo/ folder not found or podman unavailable — skipping demo UI"
fi
