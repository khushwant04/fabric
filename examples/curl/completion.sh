#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/_common.sh"

prompt="${1:-A short definition of gravity is}"
token="$(fabric_token fabric-inference)"
body="$(jq -cn --arg model "$FABRIC_MODEL" --arg prompt "$prompt" \
  '{model:$model,prompt:$prompt,max_tokens:128}')"
printf '%s' "$body" | fabric_curl "$token" \
  --fail-with-body --silent --show-error \
  --header 'Content-Type: application/json' \
  --data-binary @- \
  "$FABRIC_INFERENCE_URL/v1/completions" | jq .
