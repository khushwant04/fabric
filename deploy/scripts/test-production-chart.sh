#!/usr/bin/env bash
# Render production-shaped charts and assert immutable images plus private admin access.
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
DIGEST=sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

helm template cp "$ROOT/deploy/helm/fabric-control-plane" \
  --set database.url=sqlite+aiosqlite:///tmp/test.db \
  --set signingKey.value=test \
  --set credentialPepper=test \
  --set jwt.issuer=https://control.example \
  --set auth0.issuer=https://identity.example \
  --set auth0.audience=fabric \
  --set image.digest="$DIGEST" >"$work/control.yaml"

helm template st "$ROOT/deploy/helm/fabric-stamp" \
  --set controlPlane.url=https://control.example \
  --set controlPlane.jwtIssuer=https://control.example \
  --set enrollment.token=test \
  --set modelHost.url=http://model-host.example \
  --set image.agent.digest="$DIGEST" \
  --set image.dataPlane.digest="$DIGEST" \
  --set operator.enabled=true \
  --set monitoring.enabled=true \
  --set monitoring.rules.enabled=true >"$work/stamp.yaml"

expected='@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
[ "$(grep -c "$expected" "$work/control.yaml")" -ge 2 ]
[ "$(grep -c "$expected" "$work/stamp.yaml")" -ge 4 ]

# The admin listener is pod-local. No Service may publish its port or target its name.
if grep -A30 '^kind: Service$' "$work/stamp.yaml" | grep -Eq 'targetPort: admin|port: 8081'; then
  echo 'admin listener was exposed by a Service' >&2
  exit 1
fi

grep -q 'alert: FabricVerificationNotSynced' "$work/stamp.yaml"
echo 'production chart rendering is immutable and keeps admin private'
