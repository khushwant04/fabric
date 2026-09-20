#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/_common.sh"

prompt="${1:-Describe a calm ocean in three sentences.}"
token="$(fabric_token fabric-inference)"
body="$(jq -cn --arg model "$FABRIC_MODEL" --arg prompt "$prompt" \
  '{model:$model,messages:[{role:"user",content:$prompt}],max_tokens:256,stream:true,chat_template_kwargs:{enable_thinking:false}}')"
printf '%s' "$body" | fabric_curl "$token" \
  --no-buffer --fail-with-body --silent --show-error \
  --header 'Content-Type: application/json' \
  --data-binary @- \
  "$FABRIC_INFERENCE_URL/v1/chat/completions"
