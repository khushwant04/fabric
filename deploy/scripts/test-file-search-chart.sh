#!/usr/bin/env bash
# Render the stamp chart's hosted-tool settings: file search storage and MCP connectors.
set -euo pipefail

HELM=${HELM:-helm}
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
CHART="$ROOT/deploy/helm/fabric-stamp"
TMP=$(mktemp -d "${TMPDIR:-/tmp}/fabric-file-search-chart.XXXXXX")
trap 'rm -rf "$TMP"' EXIT

common=(
  --set controlPlane.url=https://fabric.example
  --set controlPlane.jwtIssuer=https://fabric.example
  --set enrollment.existingSecret=fabric-enrollment
  --set modelHost.url=http://model-host:8000
)

"$HELM" lint "$CHART" "${common[@]}" >/dev/null
"$HELM" lint "$CHART" "${common[@]}" --set dataPlane.fileSearch.persistence.enabled=true \
  --set 'dataPlane.mcp.allowedHosts={mcp.example.com}' >/dev/null

# Default: enabled on an emptyDir, no claim, a warning in the notes, MCP off.
"$HELM" template ephemeral "$CHART" "${common[@]}" >"$TMP/default.yaml"
grep -q 'value: /var/lib/fabric-files/store' "$TMP/default.yaml"
grep -q 'mountPath: /var/lib/fabric-files' "$TMP/default.yaml"
grep -A2 'name: file-search$' "$TMP/default.yaml" | grep -q 'emptyDir: {}'
if grep -q 'claimName: ephemeral-fabric-stamp-file-search' "$TMP/default.yaml"; then
  echo 'file search rendered a claim without persistence' >&2
  exit 1
fi
if grep -q 'FABRIC_DP_MCP_ALLOWED_HOSTS' "$TMP/default.yaml"; then
  echo 'MCP connectors are enabled by default' >&2
  exit 1
fi
grep -q 'value: "10485760"' "$TMP/default.yaml"
# NOTES.txt renders only against a cluster; assert the warning is wired to the setting.
grep -q 'not .Values.dataPlane.fileSearch.persistence.enabled' "$CHART/templates/NOTES.txt"
grep -q 'WARNING: dataPlane.fileSearch.persistence.enabled is false' "$CHART/templates/NOTES.txt"

# Persistence: a retained claim mounted at the store path.
"$HELM" template durable "$CHART" "${common[@]}" \
  --set dataPlane.fileSearch.persistence.enabled=true \
  --set dataPlane.fileSearch.persistence.size=20Gi \
  --set dataPlane.fileSearch.embeddingModel=embed-small >"$TMP/durable.yaml"
grep -q 'name: durable-fabric-stamp-file-search' "$TMP/durable.yaml"
grep -q 'claimName: durable-fabric-stamp-file-search' "$TMP/durable.yaml"
grep -q 'storage: 20Gi' "$TMP/durable.yaml"
grep -A1 'FABRIC_DP_FILE_SEARCH_EMBEDDING_MODEL' "$TMP/durable.yaml" | grep -q '"embed-small"'

# An existing claim is used as is.
"$HELM" template existing "$CHART" "${common[@]}" \
  --set dataPlane.fileSearch.persistence.enabled=true \
  --set dataPlane.fileSearch.persistence.existingClaim=kb-volume >"$TMP/existing.yaml"
grep -q 'claimName: kb-volume' "$TMP/existing.yaml"
if grep -q 'name: existing-fabric-stamp-file-search' "$TMP/existing.yaml"; then
  echo 'existing file search claim unexpectedly rendered a new PVC' >&2
  exit 1
fi

# Disabled: no env, no volume.
"$HELM" template off "$CHART" "${common[@]}" --set dataPlane.fileSearch.enabled=false >"$TMP/off.yaml"
if grep -q 'FABRIC_DP_FILE_SEARCH_STORE_PATH\|name: file-search$' "$TMP/off.yaml"; then
  echo 'disabled file search still rendered its store' >&2
  exit 1
fi

# MCP: the allowlist renders as one comma-separated value.
"$HELM" template mcp "$CHART" "${common[@]}" \
  --set 'dataPlane.mcp.allowedHosts={mcp.example.com,tools.example.org}' >"$TMP/mcp.yaml"
grep -A1 'FABRIC_DP_MCP_ALLOWED_HOSTS' "$TMP/mcp.yaml" | grep -q '"mcp.example.com,tools.example.org"'
grep -A1 'FABRIC_DP_MCP_ALLOW_INSECURE_HTTP' "$TMP/mcp.yaml" | grep -q '"false"'

echo 'file search persistence and MCP settings render as expected'
