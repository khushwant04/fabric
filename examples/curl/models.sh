#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/_common.sh"

token="$(fabric_token fabric-inference)"
fabric_curl "$token" \
  --fail-with-body --silent --show-error \
  "$FABRIC_INFERENCE_URL/v1/models" | jq .
