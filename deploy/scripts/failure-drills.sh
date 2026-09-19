#!/usr/bin/env bash
# Repeatable, bounded production drills. Run one drill at a time; every mutation has a trap.
set -Eeuo pipefail

DRILL=${1:-}
CP_NAMESPACE=${CP_NAMESPACE:-fabric-control}
CP_DEPLOYMENT=${CP_DEPLOYMENT:-cp-fabric-control-plane}
STAMP_NAMESPACE=${STAMP_NAMESPACE:-fabric-stamp}
STAMP_STATEFULSET=${STAMP_STATEFULSET:-st-fabric-stamp}
STAMP_POD=${STAMP_POD:-st-fabric-stamp-0}
DP_URL=${DP_URL:-https://inference.hexelstudio.com}
CP_URL=${CP_URL:-https://fabric-cp.hexelstudio.com}
TIMEOUT=${TIMEOUT:-10m}

usage() {
  echo "usage: $0 verification|cp-outage|agent-restart|model-rollout" >&2
  exit 2
}
[ -n "$DRILL" ] || usage

verification() {
  local body
  body=$(kubectl exec -n "$STAMP_NAMESPACE" "$STAMP_POD" -c data-plane -- \
    python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8081/admin/verification').read().decode())")
  python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["source"]=="synced",d; assert d["rejected_updates"]==0,d; print("verification: synced")' <<<"$body"
}

inference_canary() {
  : "${INFERENCE_TOKEN:?set a short-lived INFERENCE_TOKEN before this drill}"
  curl -fsS "$DP_URL/v1/models" -H "authorization: Bearer $INFERENCE_TOKEN" \
    | python3 -c 'import json,sys; d=json.load(sys.stdin); assert d["data"]; print("inference: serving from local verification")'
}

CP_ORIGINAL_REPLICAS=""
restore_cp() {
  trap - EXIT
  [ -n "$CP_ORIGINAL_REPLICAS" ] || return 0
  kubectl scale deployment "$CP_DEPLOYMENT" -n "$CP_NAMESPACE" \
    --replicas="$CP_ORIGINAL_REPLICAS" >/dev/null
  kubectl rollout status deployment/"$CP_DEPLOYMENT" -n "$CP_NAMESPACE" \
    --timeout="$TIMEOUT" >/dev/null
}

cp_outage() {
  CP_ORIGINAL_REPLICAS=$(kubectl get deployment "$CP_DEPLOYMENT" -n "$CP_NAMESPACE" -o jsonpath='{.spec.replicas}')
  trap restore_cp EXIT
  # Warm and prove the token before imposing failure.
  inference_canary
  kubectl scale deployment "$CP_DEPLOYMENT" -n "$CP_NAMESPACE" --replicas=0 >/dev/null
  # Migration Jobs share the app name. Select only deployment pods, which have no
  # component label, or completed migration pods make this wait time out forever.
  kubectl wait --for=delete pod -n "$CP_NAMESPACE" \
    -l 'app.kubernetes.io/name=fabric-control-plane,!app.kubernetes.io/component' \
    --timeout=3m >/dev/null
  inference_canary
  # An outage must never become fail-open.
  code=$(curl -sS -o /dev/null -w '%{http_code}' "$DP_URL/v1/models" \
    -H 'authorization: Bearer definitely-invalid')
  [ "$code" = 401 ] || { echo "invalid token returned $code during outage" >&2; exit 1; }
  restore_cp
  curl -fsS "$CP_URL/readyz" >/dev/null
  echo 'cp-outage: cached-key inference survived and invalid tokens stayed closed'
}

agent_restart() {
  local stamp_id
  stamp_id=$(kubectl exec -n "$STAMP_NAMESPACE" "$STAMP_POD" -c agent -- \
    /usr/local/bin/fabric-agent --version 2>/dev/null || true)
  kubectl delete pod "$STAMP_POD" -n "$STAMP_NAMESPACE" --wait=true >/dev/null
  kubectl rollout status statefulset/"$STAMP_STATEFULSET" -n "$STAMP_NAMESPACE" --timeout="$TIMEOUT" >/dev/null
  verification
  if [ -n "${INFERENCE_TOKEN:-}" ]; then inference_canary; fi
  echo "agent-restart: durable state recovered${stamp_id:+ (agent $stamp_id)}"
}

model_rollout() {
  : "${CONTROL_TOKEN:?set a short-lived control-plane bearer token}"
  : "${ACCOUNT_ID:?set ACCOUNT_ID}"
  : "${DEPLOYMENT_ID:?set DEPLOYMENT_ID for a staging-safe deployment}"
  [ "${ALLOW_MODEL_ROLLOUT:-}" = 'yes' ] || {
    echo 'set ALLOW_MODEL_ROLLOUT=yes after confirming spare GPU capacity' >&2
    exit 2
  }
  local url work generation observed restore_generation
  url="$CP_URL/v1/accounts/$ACCOUNT_ID/deployments/$DEPLOYMENT_ID"
  work=$(mktemp -d)
  chmod 700 "$work"
  wait_for_generation() {
    local wanted=$1 current=0
    for _ in $(seq 1 90); do
      current=$(curl -fsS "$url/status" -H "authorization: Bearer $CONTROL_TOKEN" \
        | python3 -c 'import json,sys; rows=json.load(sys.stdin); print(max((x.get("observed_generation",0) for x in rows),default=0))')
      [ "$current" -ge "$wanted" ] && return 0
      sleep 10
    done
    echo "generation $wanted did not converge (last observed $current)" >&2
    return 1
  }
  restore() {
    [ -s "$work/original-request.json" ] || return 0
    curl -fsS -X PATCH "$url" -H "authorization: Bearer $CONTROL_TOKEN" \
      -H 'content-type: application/json' --data-binary @"$work/original-request.json" \
      >"$work/restored.json"
    restore_generation=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["generation"])' "$work/restored.json")
    wait_for_generation "$restore_generation"
  }
  cleanup_on_exit() {
    status=$?
    if [ -s "$work/original-request.json" ]; then
      if ! restore; then
        echo "CRITICAL: failed to restore deployment; diagnostics retained at $work" >&2
        exit 1
      fi
    fi
    rm -rf "$work"
    return "$status"
  }
  trap cleanup_on_exit EXIT
  curl -fsS "$url" -H "authorization: Bearer $CONTROL_TOKEN" >"$work/deployment.json"
  python3 - "$work" <<'PY'
import json, pathlib, sys
p=pathlib.Path(sys.argv[1])
d=json.loads((p/'deployment.json').read_text())
spec=d['spec']
(p/'original-spec.json').write_text(json.dumps(spec))
(p/'original-request.json').write_text(json.dumps({'spec': spec}))
runtime=spec['runtime']
runtime['max_num_seqs']=2 if runtime.get('max_num_seqs') != 2 else 3
(p/'changed-request.json').write_text(json.dumps({'spec': spec}))
PY
  curl -fsS -X PATCH "$url" -H "authorization: Bearer $CONTROL_TOKEN" \
    -H 'content-type: application/json' --data-binary @"$work/changed-request.json" >"$work/changed.json"
  generation=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["generation"])' "$work/changed.json")
  wait_for_generation "$generation"
  if [ -n "${INFERENCE_TOKEN:-}" ]; then inference_canary; fi
  restore
  trap - EXIT
  rm -rf "$work"
  echo 'model-rollout: changed and restored generations both converged'
}

case "$DRILL" in
  verification) verification ;;
  cp-outage) cp_outage ;;
  agent-restart) agent_restart ;;
  model-rollout) model_rollout ;;
  *) usage ;;
esac
