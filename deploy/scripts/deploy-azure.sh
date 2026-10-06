#!/usr/bin/env bash
# Deploy already-built Fabric images by immutable digest in the only safe order.
set -Eeuo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
CP_CHART="$ROOT/deploy/helm/fabric-control-plane"
STAMP_CHART="$ROOT/deploy/helm/fabric-stamp"

: "${CONTROL_PLANE_DIGEST:?set CONTROL_PLANE_DIGEST=sha256:...}"
: "${AGENT_DIGEST:?set AGENT_DIGEST=sha256:...}"
: "${DATA_PLANE_DIGEST:?set DATA_PLANE_DIGEST=sha256:...}"

CP_RELEASE=${CP_RELEASE:-cp}
CP_NAMESPACE=${CP_NAMESPACE:-fabric-control}
STAMP_RELEASE=${STAMP_RELEASE:-st}
STAMP_NAMESPACE=${STAMP_NAMESPACE:-fabric-stamp}
TIMEOUT=${TIMEOUT:-15m}

require_digest() {
  [[ "$2" =~ ^sha256:[0-9a-f]{64}$ ]] || {
    echo "$1 must be sha256 followed by 64 lowercase hexadecimal characters" >&2
    exit 2
  }
}
require_digest CONTROL_PLANE_DIGEST "$CONTROL_PLANE_DIGEST"
require_digest AGENT_DIGEST "$AGENT_DIGEST"
require_digest DATA_PLANE_DIGEST "$DATA_PLANE_DIGEST"
if [[ -n ${CONSOLE_DIGEST:-} ]]; then
  require_digest CONSOLE_DIGEST "$CONSOLE_DIGEST"
fi

for command in helm kubectl python3; do
  command -v "$command" >/dev/null || { echo "$command is required" >&2; exit 2; }
done

# Computed values can contain inline credentials as well as Secret references.
# Files must be private from their creation, including before chmod could run.
umask 077
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
chmod 700 "$work"

# Preserve the resolved configuration of both installed releases. Resetting to the
# current chart defaults and overlaying these values adds new optional settings
# without reusing the old chart's defaults (which lack newly introduced maps).
# Capture both before any mutation, so a missing release fails without upgrading one.
helm get values "$CP_RELEASE" -n "$CP_NAMESPACE" -a -o yaml >"$work/control-values.yaml"
helm get values "$STAMP_RELEASE" -n "$STAMP_NAMESPACE" -a -o yaml >"$work/stamp-values.yaml"

# A chart-created enrollment Secret survives Helm uninstall. Keep its reference and
# never restore a spent one-time token from Helm's historical values.
stamp_enrollment_secret=$(helm template "$STAMP_RELEASE" "$STAMP_CHART" \
  -n "$STAMP_NAMESPACE" -f "$work/stamp-values.yaml" \
  --show-only templates/statefulset.yaml | python3 -c '
import re,sys
manifest=sys.stdin.read()
match=re.search(r"name: FABRIC_AGENT_ENROLLMENT_TOKEN\s+valueFrom:\s+secretKeyRef:\s+name: ([^\s]+)", manifest)
if match is None:
    raise SystemExit("cannot determine the retained enrollment Secret")
print(match.group(1).strip("\""))
')
stamp_upgrade_args=(--reset-values -f "$work/stamp-values.yaml" \
  --set enrollment.token='' --set-string enrollment.existingSecret="$stamp_enrollment_secret")

printf '1/4 Establishing CRD schema before any new agent can publish fields...\n'
helm template "$STAMP_RELEASE" "$STAMP_CHART" \
  -n "$STAMP_NAMESPACE" -f "$work/stamp-values.yaml" \
  --set operator.installCRD=true --show-only templates/crd.yaml | kubectl apply -f -
kubectl wait --for=condition=Established --timeout=2m \
  crd/fabricmodeldeployments.fabric.khushwant.dev

printf '2/4 Deploying control plane by digest...\n'
control_image_args=(--set-string image.digest="$CONTROL_PLANE_DIGEST")
if [[ -n ${CONSOLE_DIGEST:-} ]]; then
  # Preserve console.enabled and its runtime Secret/URL configuration. Existing
  # control-plane-only installations do not acquire a console during an upgrade.
  control_image_args+=(--set-string console.image.digest="$CONSOLE_DIGEST")
fi
helm upgrade "$CP_RELEASE" "$CP_CHART" -n "$CP_NAMESPACE" \
  --reset-values -f "$work/control-values.yaml" \
  "${control_image_args[@]}" --atomic --wait --timeout "$TIMEOUT"
kubectl rollout status deployment/"$CP_RELEASE-fabric-control-plane" \
  -n "$CP_NAMESPACE" --timeout="$TIMEOUT"

printf '3/4 Deploying agent and operator while retaining the current data plane...\n'
helm upgrade "$STAMP_RELEASE" "$STAMP_CHART" -n "$STAMP_NAMESPACE" \
  "${stamp_upgrade_args[@]}" \
  --set-string image.agent.digest="$AGENT_DIGEST" \
  --atomic --wait --timeout "$TIMEOUT"
kubectl rollout status deployment/"$STAMP_RELEASE-fabric-stamp-operator" \
  -n "$STAMP_NAMESPACE" --timeout="$TIMEOUT"
kubectl rollout status statefulset/"$STAMP_RELEASE-fabric-stamp" \
  -n "$STAMP_NAMESPACE" --timeout="$TIMEOUT"

# Unknown CR fields are silently pruned by Kubernetes. Refuse to continue unless the
# authoritative verification pair survived storage after the agent upgrade.
kubectl get fabricmodeldeployments.fabric.khushwant.dev -n "$STAMP_NAMESPACE" -o json \
  | python3 -c 'import json,sys; items=json.load(sys.stdin)["items"]; bad=[x["metadata"]["name"] for x in items if not x.get("spec",{}).get("jwtIssuer","").startswith("https://") or not x.get("spec",{}).get("jwksUrl","").startswith("https://")]; assert not bad, f"missing HTTPS verification contract: {bad}"'

printf '4/4 Deploying data plane by digest and checking synchronized verification...\n'
helm upgrade "$STAMP_RELEASE" "$STAMP_CHART" -n "$STAMP_NAMESPACE" \
  "${stamp_upgrade_args[@]}" \
  --set-string image.agent.digest="$AGENT_DIGEST" \
  --set-string image.dataPlane.digest="$DATA_PLANE_DIGEST" \
  --atomic --wait --timeout "$TIMEOUT"
kubectl rollout status statefulset/"$STAMP_RELEASE-fabric-stamp" \
  -n "$STAMP_NAMESPACE" --timeout="$TIMEOUT"

pod="$STAMP_RELEASE-fabric-stamp-0"
verification=$(kubectl exec -n "$STAMP_NAMESPACE" "$pod" -c data-plane -- \
  python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8081/admin/verification').read().decode())")
python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["source"] == "synced", d; assert d["rejected_updates"] == 0, d' \
  <<<"$verification"

printf 'Deployment complete: verification is synced and no update was rejected.\n'
