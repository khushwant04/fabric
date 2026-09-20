#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/_common.sh"

token_response="$(fabric_token_response fabric-control)"
token="$(jq -er '.access_token | select(type == "string" and test("^[A-Za-z0-9._~-]+$"))' \
  <<<"$token_response")"
account_id="$(jq -er '.account_id' <<<"$token_response")"

fabric_curl "$token" --fail-with-body --silent --show-error \
  "$FABRIC_CONTROL_URL/v1/self" | jq .
fabric_curl "$token" --fail-with-body --silent --show-error \
  "$FABRIC_CONTROL_URL/v1/accounts/$account_id" | jq .
fabric_curl "$token" --fail-with-body --silent --show-error \
  "$FABRIC_CONTROL_URL/v1/accounts/$account_id/deployments" | jq .

deployment_id="${1:-}"
if [[ -n "$deployment_id" ]]; then
  base="$FABRIC_CONTROL_URL/v1/accounts/$account_id/deployments/$deployment_id"
  fabric_curl "$token" --fail-with-body --silent --show-error "$base" | jq .
  fabric_curl "$token" --fail-with-body --silent --show-error "$base/status" | jq .
  fabric_curl "$token" --fail-with-body --silent --show-error "$base/usage" | jq .
fi
