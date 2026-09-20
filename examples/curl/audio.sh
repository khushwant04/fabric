#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "$0")/_common.sh"

operation="${1:-transcriptions}"
case "$operation" in transcriptions|translations) ;; *) echo 'usage: audio.sh [transcriptions|translations]' >&2; exit 2 ;; esac
: "${FABRIC_AUDIO_FILE:?Set FABRIC_AUDIO_FILE to a local audio file}"
token="$(fabric_token fabric-inference)"
fabric_curl "$token" \
  --fail-with-body --silent --show-error \
  --form "model=$FABRIC_MODEL" \
  --form "file=@${FABRIC_AUDIO_FILE}" \
  "$FABRIC_INFERENCE_URL/v1/audio/$operation" | jq -R 'fromjson? // .'
