#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/_common.sh"

prompt="${1:-Explain why leaves change color.}"
token="$(fabric_token fabric-inference)"
body="$(jq -cn --arg model "$FABRIC_MODEL" --arg prompt "$prompt" \
  '{model:$model,messages:[{role:"user",content:$prompt}],max_tokens:256,chat_template_kwargs:{enable_thinking:false}}')"
printf '%s' "$body" | fabric_curl "$token" \
  --fail-with-body --silent --show-error \
  --header 'Content-Type: application/json' \
  --data-binary @- \
  "$FABRIC_INFERENCE_URL/v1/chat/completions" | jq .
