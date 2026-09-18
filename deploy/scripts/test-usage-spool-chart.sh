#!/usr/bin/env bash
set -euo pipefail

HELM=${HELM:-helm}
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
CHART="$ROOT/deploy/helm/fabric-stamp"
TMP=$(mktemp -d "${TMPDIR:-/tmp}/fabric-spool-chart.XXXXXX")
trap 'rm -rf "$TMP"' EXIT

common=(
  --set controlPlane.url=https://fabric.example
  --set controlPlane.jwtIssuer=https://fabric.example
  --set enrollment.existingSecret=fabric-enrollment
  --set modelHost.url=http://model-host:8000
)

"$HELM" lint "$CHART" "${common[@]}" >/dev/null

"$HELM" template durable "$CHART" "${common[@]}" >"$TMP/durable.yaml"
grep -q '^kind: PersistentVolumeClaim$' "$TMP/durable.yaml"
grep -q 'helm.sh/resource-policy: keep' "$TMP/durable.yaml"
grep -q 'value: /var/lib/fabric-usage/private/usage.db' "$TMP/durable.yaml"
grep -q 'mountPath: /var/lib/fabric-usage' "$TMP/durable.yaml"
grep -q 'claimName: durable-fabric-stamp-usage-spool' "$TMP/durable.yaml"

"$HELM" template existing "$CHART" "${common[@]}" \
  --set usageSpool.existingClaim=preprovisioned-usage >"$TMP/existing.yaml"
if grep -q '^kind: PersistentVolumeClaim$' "$TMP/existing.yaml"; then
  echo 'existing usage claim unexpectedly rendered a new PVC' >&2
  exit 1
fi
grep -q 'claimName: preprovisioned-usage' "$TMP/existing.yaml"

"$HELM" template ephemeral "$CHART" "${common[@]}" \
  --set usageSpool.enabled=false >"$TMP/ephemeral.yaml"
if grep -q '^kind: PersistentVolumeClaim$' "$TMP/ephemeral.yaml"; then
  echo 'ephemeral usage spool unexpectedly rendered a PVC' >&2
  exit 1
fi
grep -A2 'name: usage-spool' "$TMP/ephemeral.yaml" | grep -q 'emptyDir: {}'

echo 'usage spool Helm variants passed'
