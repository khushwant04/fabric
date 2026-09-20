#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/_common.sh"

: "${FABRIC_IMAGE_FILE:?Set FABRIC_IMAGE_FILE to a local image file}"
prompt="${1:-Describe this image concisely.}"
case "${FABRIC_IMAGE_FILE##*.}" in
  jpg|jpeg|JPG|JPEG) media_type='image/jpeg' ;;
  png|PNG) media_type='image/png' ;;
  gif|GIF) media_type='image/gif' ;;
  webp|WEBP) media_type='image/webp' ;;
  *) echo 'error: use a JPEG, PNG, GIF, or WebP image' >&2; exit 2 ;;
esac

image_b64="$(base64 <"$FABRIC_IMAGE_FILE" | tr -d '\n')"
token="$(fabric_token fabric-inference)"
body="$(jq -cn \
  --arg model "$FABRIC_MODEL" \
  --arg prompt "$prompt" \
  --arg image "data:$media_type;base64,$image_b64" \
  '{model:$model,messages:[{role:"user",content:[{type:"text",text:$prompt},{type:"image_url",image_url:{url:$image}}]}],max_tokens:256,chat_template_kwargs:{enable_thinking:false}}')"

printf '%s' "$body" | fabric_curl "$token" \
  --fail-with-body --silent --show-error \
  --header 'Content-Type: application/json' \
  --data-binary @- \
  "$FABRIC_INFERENCE_URL/v1/chat/completions" | jq .
