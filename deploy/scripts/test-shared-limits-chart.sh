#!/usr/bin/env bash
set -euo pipefail

HELM=${HELM:-helm}
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
CHART="$ROOT/deploy/helm/fabric-stamp"
TMP=$(mktemp -d "${TMPDIR:-/tmp}/fabric-limits-chart.XXXXXX")
trap 'rm -rf "$TMP"' EXIT
common=(
  --set controlPlane.url=https://fabric.example
  --set controlPlane.jwtIssuer=https://fabric.example
  --set enrollment.existingSecret=fabric-enrollment
  --set modelHost.url=http://model-host:8000
  --set dataPlane.limits.requestsPerMinute=60
  --set dataPlane.limits.burst=2
  --set dataPlane.limits.maxInFlightPerAccount=2
)

"$HELM" lint "$CHART" "${common[@]}" >/dev/null
"$HELM" template shared "$CHART" "${common[@]}" >"$TMP/shared.yaml"
grep -q 'name: shared-fabric-stamp-limits' "$TMP/shared.yaml"
grep -q 'name: shared-fabric-stamp-limit-coordinator' "$TMP/shared.yaml"
grep -q 'FABRIC_DP_LIMIT_COORDINATOR_URL' "$TMP/shared.yaml"
grep -q 'value: http://shared-fabric-stamp-limits:8083' "$TMP/shared.yaml"
grep -q 'FABRIC_DP_LIMIT_COORDINATOR_STORE_PATH' "$TMP/shared.yaml"
grep -q 'value: /var/lib/fabric-usage/private/limits.db' "$TMP/shared.yaml"
grep -q 'containerPort: 8083' "$TMP/shared.yaml"
grep -q 'port: limits' "$TMP/shared.yaml"

# Parse document-local contracts rather than only proving strings exist somewhere.
awk '
  /^# Source: fabric-stamp\/templates\/limit-coordinator-service.yaml$/ {in_doc=1; next}
  /^---$/ && in_doc {exit}
  in_doc {print}
' "$TMP/shared.yaml" >"$TMP/limits-service.yaml"
grep -q 'app.kubernetes.io/component: stamp' "$TMP/limits-service.yaml"
if grep -q 'app.kubernetes.io/component: operator\|app.kubernetes.io/component: gateway' "$TMP/limits-service.yaml"; then
  echo 'limits Service selects a non-authority pod' >&2
  exit 1
fi
awk '
  /^# Source: fabric-stamp\/templates\/service.yaml$/ {in_doc=1; next}
  /^---$/ && in_doc {exit}
  in_doc {print}
' "$TMP/shared.yaml" >"$TMP/public-service.yaml"
if grep -q 'port: limits\|8083' "$TMP/public-service.yaml"; then
  echo 'public inference Service exposes the limit coordinator' >&2
  exit 1
fi
grep -A25 'port: limits' "$TMP/shared.yaml" | grep -q 'app.kubernetes.io/component: stamp'
grep -A25 'port: limits' "$TMP/shared.yaml" | grep -q 'app.kubernetes.io/component: gateway'

"$HELM" template with-operator "$CHART" "${common[@]}" \
  --set operator.enabled=true \
  --set operator.managedModelHost.image=vllm/vllm-openai:test \
  --set operator.managedModelHost.modelRef=org/model \
  --set operator.managedModelHost.servedName=release-1 >"$TMP/operator.yaml"
grep -q 'app.kubernetes.io/component: operator' "$TMP/operator.yaml"
awk '
  /^# Source: fabric-stamp\/templates\/limit-coordinator-service.yaml$/ {in_doc=1; next}
  /^---$/ && in_doc {exit}
  in_doc {print}
' "$TMP/operator.yaml" >"$TMP/operator-limits-service.yaml"
grep -q 'app.kubernetes.io/component: stamp' "$TMP/operator-limits-service.yaml"
if grep -q 'app.kubernetes.io/component: operator' "$TMP/operator-limits-service.yaml"; then
  echo 'operator-enabled limits Service selects the operator pod' >&2
  exit 1
fi

"$HELM" template existing "$CHART" "${common[@]}" \
  --set dataPlane.limits.shared.existingSecret=stamp-limit-token >"$TMP/existing.yaml"
if grep -q '^kind: Secret$' "$TMP/existing.yaml"; then
  echo 'existing limit token unexpectedly rendered a Secret' >&2
  exit 1
fi
grep -q 'name: stamp-limit-token' "$TMP/existing.yaml"

"$HELM" template local "$CHART" "${common[@]}" \
  --set dataPlane.limits.shared.enabled=false >"$TMP/local.yaml"
if grep -q 'FABRIC_DP_LIMIT_COORDINATOR_URL\|containerPort: 8083\|name: local-fabric-stamp-limits' "$TMP/local.yaml"; then
  echo 'local limit mode unexpectedly rendered coordinator resources' >&2
  exit 1
fi

if "$HELM" template invalid "$CHART" "${common[@]}" \
  --set dataPlane.limits.shared.leaseSeconds=60 \
  --set dataPlane.limits.shared.renewSeconds=30 >/dev/null 2>&1; then
  echo 'invalid lease renewal interval unexpectedly rendered' >&2
  exit 1
fi

echo 'shared limit Helm variants passed'
