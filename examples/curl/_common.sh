#!/usr/bin/env bash
set -euo pipefail

USER_AGENT='fabric-api-examples/1.0'
: "${FABRIC_CONTROL_URL:?Set FABRIC_CONTROL_URL}"
: "${FABRIC_INFERENCE_URL:?Set FABRIC_INFERENCE_URL}"
: "${FABRIC_API_KEY:?Set FABRIC_API_KEY}"
FABRIC_MODEL="${FABRIC_MODEL:-qwen3.5-0.8b}"
FABRIC_CONTROL_URL="${FABRIC_CONTROL_URL%/}"
FABRIC_INFERENCE_URL="${FABRIC_INFERENCE_URL%/}"
FABRIC_CONTROL_URL="${FABRIC_CONTROL_URL%/v1}"
FABRIC_INFERENCE_URL="${FABRIC_INFERENCE_URL%/v1}"

# Keep the long-lived key in this shell only. Child processes should receive credentials
# through stdin/file descriptors rather than environment variables or command arguments.
export -n FABRIC_API_KEY

command -v jq >/dev/null || { echo 'error: jq is required' >&2; exit 2; }

fabric_token_response() {
  local audience="$1"
  printf '%s\n' "$FABRIC_API_KEY" \
    | jq -Rn --arg audience "$audience" \
      'input as $key | {grant_type:"api_key",api_key:$key,audience:$audience}' \
    | curl --fail-with-body --silent --show-error \
      --user-agent "$USER_AGENT" \
      --header 'Content-Type: application/json' \
      --data-binary @- \
      "$FABRIC_CONTROL_URL/v1/token"
}

fabric_token() {
  local response token
  response="$(fabric_token_response "$1")"
  token="$(jq -er '.access_token | select(type == "string" and test("^[A-Za-z0-9._~-]+$"))' \
    <<<"$response")"
  printf '%s\n' "$token"
}

fabric_curl() {
  local token="$1"
  shift
  # Process substitution exposes only a file descriptor in curl's argv. The bearer value is
  # consumed from that descriptor and is not inherited in curl's environment.
  curl --config <(
    printf 'user-agent = "%s"\n' "$USER_AGENT"
    printf 'header = "Authorization: Bearer %s"\n' "$token"
  ) "$@"
}
