#!/usr/bin/env bash
#
# Deploy a stamp to a throwaway Kubernetes cluster and drive the whole loop.
#
# agent/scripts/e2e.sh proves the components agree as processes. This proves the
# packaging: images that run as non-root, a chart whose values reach the containers,
# probes the kubelet can actually reach, and state that survives a pod restart.
# Several defects were only ever visible here, including a wrong environment prefix
# that left the data plane verifying tokens against its default issuer, and a
# restarted agent that served nothing because its rendered configuration was gone
# while its acknowledged generation was not.
#
#   deploy/scripts/kind-e2e.sh
#
# Requires docker, kind, kubectl, helm, and the control-plane environment. The
# control plane runs on the host against FABRIC_DATABASE_URL, which must point at a
# database whose contents can be discarded; PostgreSQL exercises the engine used in
# production, and SQLite works too.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CLUSTER="${FABRIC_KIND_CLUSTER:-fabric-e2e}"
# With the operator, the agent declares FabricModelDeployment resources and the
# operator renders the data plane's configuration. Without it, the agent writes that
# file itself. Both paths ship, so both are exercised.
OPERATOR="${FABRIC_E2E_OPERATOR:-0}"
WORK="$(mktemp -d)"
CONTROL_PID=""
KEEP="${FABRIC_KEEP_CLUSTER:-0}"

log()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
info() { printf '   %s\n' "$*"; }
die()  { printf '\033[31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

cleanup() {
    if [ -n "$CONTROL_PID" ]; then
        kill "$CONTROL_PID" 2>/dev/null || true
        for _ in $(seq 1 20); do
            kill -0 "$CONTROL_PID" 2>/dev/null || break
            sleep 0.1
        done
        kill -9 "$CONTROL_PID" 2>/dev/null || true
    fi
    if [ "$KEEP" != "1" ]; then
        kind delete cluster --name "$CLUSTER" >/dev/null 2>&1 || true
    else
        info "cluster $CLUSTER kept (FABRIC_KEEP_CLUSTER=1)"
    fi
    rm -rf "$WORK"
}
trap cleanup EXIT

for tool in docker kind kubectl helm; do
    command -v "$tool" >/dev/null || die "$tool is required"
done
CONTROL_PY="$REPO_ROOT/control-plane/.venv/bin/python"
[ -x "$CONTROL_PY" ] || die "control-plane environment missing"

KUBECTL=(kubectl --context "kind-$CLUSTER")

# --------------------------------------------------------------------------
log "1. Build images"
# --------------------------------------------------------------------------

for image in agent data-plane; do
    docker build -q -f "$REPO_ROOT/deploy/images/$image.Dockerfile" \
        -t "fabric/$image:e2e" "$REPO_ROOT" >/dev/null
    info "built fabric/$image:e2e"
done

# Both images must run unprivileged; a chart asking for runAsNonRoot against an
# image that only works as root fails at admission, not at build.
for binary in fabric-agent fabric-collector fabric-operator; do
    docker run --rm --entrypoint "/usr/local/bin/$binary" fabric/agent:e2e --version >/dev/null \
        || die "$binary is missing from the image"
done
info "agent, collector, and operator binaries start"

# --------------------------------------------------------------------------
log "2. Create the cluster and load the images"
# --------------------------------------------------------------------------

kind create cluster --name "$CLUSTER" --wait 180s >/dev/null 2>&1 \
    || die "could not create cluster $CLUSTER"
kind load docker-image "fabric/agent:e2e" "fabric/data-plane:e2e" --name "$CLUSTER" >/dev/null
info "cluster ready: $("${KUBECTL[@]}" get nodes -o name | tr '\n' ' ')"

"${KUBECTL[@]}" apply -f "$REPO_ROOT/deploy/testing/stub-model-host.yaml" >/dev/null
"${KUBECTL[@]}" wait --for=condition=available deployment/stub-model-host --timeout=180s >/dev/null
info "stub model host running (not a product component)"

# --------------------------------------------------------------------------
log "3. Control plane on the host, reachable from the cluster"
# --------------------------------------------------------------------------

GATEWAY="$(docker network inspect kind -f '{{range .IPAM.Config}}{{.Gateway}}
{{end}}' | grep -E '^[0-9]+\.' | head -1)"
[ -n "$GATEWAY" ] || die "could not determine the cluster's route to the host"

PORT="${FABRIC_E2E_PORT:-$("$CONTROL_PY" -c '
import socket
s = socket.socket()
s.bind(("0.0.0.0", 0))
print(s.getsockname()[1])
s.close()')}"
CONTROL_URL="http://$GATEWAY:$PORT"

openssl genrsa -out "$WORK/signing.pem" 2048 2>/dev/null
export FABRIC_APP_ENV=test
export FABRIC_DATABASE_URL="${FABRIC_DATABASE_URL:-sqlite+aiosqlite:///$WORK/kind-e2e.db}"
export FABRIC_JWT_ISSUER="$CONTROL_URL"
export FABRIC_AUTH0_AUDIENCE="unused-in-this-path"
export FABRIC_CREDENTIAL_PEPPER="kind-e2e-pepper-not-a-real-secret"
export FABRIC_JWT_PRIVATE_KEY_PATH="$WORK/signing.pem"
case "$FABRIC_DATABASE_URL" in
    sqlite*) info "database: sqlite (set FABRIC_DATABASE_URL to exercise PostgreSQL)" ;;
    *)       info "database: PostgreSQL" ;;
esac

cd "$REPO_ROOT/control-plane"
"$CONTROL_PY" -m alembic upgrade head >/dev/null 2>&1 || die "migrations failed"

SUFFIX="$RANDOM$RANDOM"
"$CONTROL_PY" - "$SUFFIX" > "$WORK/seed.json" <<'PY'
import asyncio, json, sys
from sqlalchemy import update
from app.core.database import dispose_engine, get_session_factory
from app.models import Account, User
from app.services.accounts import create_account, ensure_system_account
from app.services.api_keys import create_api_key

suffix = sys.argv[1]

async def main():
    factory = get_session_factory()
    async with factory() as session:
        await ensure_system_account(session)
        user = User(auth0_subject=f"kind|{suffix}", email=f"op-{suffix}@kind.test")
        session.add(user)
        await session.flush()
        account, _ = await create_account(
            session, slug=f"kind-{suffix}", name="Kind", owner=user
        )
        await session.execute(
            update(Account).where(Account.id == account.id)
            .values(managed_capacity_enabled=True)
        )
        scopes = [
            "deployments:read", "deployments:write",
            "stamps:read", "stamps:write", "inference:invoke",
        ]
        _record, secret = await create_api_key(
            session, account_id=account.id, name="kind", requested_scopes=scopes,
            principal_scopes=frozenset(scopes[:-1]), expires_in_days=None,
            service_principal_id=None, actor_user_id=user.id,
        )
        await session.commit()
        print(json.dumps({"account_id": str(account.id), "api_key": secret}))
    await dispose_engine()

asyncio.run(main())
PY

# Bound to every interface so the cluster can reach it over the bridge.
"$CONTROL_PY" -m uvicorn app.main:app --host 0.0.0.0 --port "$PORT" \
    --log-level warning >"$WORK/control-plane.log" 2>&1 &
CONTROL_PID=$!
cd "$REPO_ROOT"

for _ in $(seq 1 60); do
    curl -fsS "$CONTROL_URL/healthz" >/dev/null 2>&1 && break
    sleep 0.5
done
curl -fsS "$CONTROL_URL/healthz" >/dev/null \
    || { cat "$WORK/control-plane.log"; die "control plane did not start"; }
info "listening on $CONTROL_URL"

ACCOUNT_ID=$("$CONTROL_PY" -c "import json;print(json.load(open('$WORK/seed.json'))['account_id'])")
API_KEY=$("$CONTROL_PY" -c "import json;print(json.load(open('$WORK/seed.json'))['api_key'])")

token_for() {
    curl -fsS -X POST "$CONTROL_URL/v1/token" -H 'Content-Type: application/json' \
        -d "{\"grant_type\":\"api_key\",\"api_key\":\"$API_KEY\",\"audience\":\"$1\"}" \
        | "$CONTROL_PY" -c "import json,sys;print(json.load(sys.stdin)['access_token'])"
}
CONTROL_TOKEN=$(token_for fabric-control)

api() {
    local method="$1" path="$2" body="${3:-}"
    if [ -n "$body" ]; then
        curl -fsS -X "$method" "$CONTROL_URL$path" \
            -H "Authorization: Bearer $CONTROL_TOKEN" \
            -H 'Content-Type: application/json' -d "$body"
    else
        curl -fsS -X "$method" "$CONTROL_URL$path" -H "Authorization: Bearer $CONTROL_TOKEN"
    fi
}

DEPLOYMENT_ID=$(api POST "/v1/accounts/$ACCOUNT_ID/deployments" \
    '{"name":"kind-deployment","model_alias":"kind-model","spec":{"runtime":{"release":"stub-release"}}}' \
    | "$CONTROL_PY" -c "import json,sys;print(json.load(sys.stdin)['id'])")
ENROLL_TOKEN=$(api POST "/v1/accounts/$ACCOUNT_ID/stamp-enrollment-tokens" \
    '{"allowed_mode":"byoi","expires_in_minutes":30}' \
    | "$CONTROL_PY" -c "import json,sys;print(json.load(sys.stdin)['enrollment_token'])")
info "deployment $DEPLOYMENT_ID and a single-use enrollment token created"

# --------------------------------------------------------------------------
log "4. Install the chart"
# --------------------------------------------------------------------------

OPERATOR_ARGS=()
if [ "$OPERATOR" = "1" ]; then
    OPERATOR_ARGS=(--set operator.enabled=true --set operator.interval=5s)
    info "operator enabled: intent is declared as custom resources"
else
    info "operator disabled: the agent writes the data plane's file directly"
fi

helm install stamp "$REPO_ROOT/deploy/helm/fabric-stamp" \
    --kube-context "kind-$CLUSTER" \
    "${OPERATOR_ARGS[@]}" \
    --set controlPlane.url="$CONTROL_URL" \
    --set controlPlane.jwtIssuer="$CONTROL_URL" \
    --set modelHost.url=http://stub-model-host:8000 \
    --set enrollment.token="$ENROLL_TOKEN" \
    --set image.agent.repository=fabric/agent --set image.agent.tag=e2e \
    --set image.dataPlane.repository=fabric/data-plane --set image.dataPlane.tag=e2e \
    --set stamp.name=kind-stamp --set stamp.orchestrator=k3s \
    --set collector.interval=10s \
    --set persistence.size=64Mi \
    --wait --timeout 300s >/dev/null \
    || { "${KUBECTL[@]}" logs stamp-fabric-stamp-0 --all-containers --tail=30; die "install failed"; }

# Readiness requires verification keys, so the pod becoming ready proves it fetched
# them from the control plane without any request having been served.
info "all three containers ready: $("${KUBECTL[@]}" get pod stamp-fabric-stamp-0 \
    -o jsonpath='{range .status.containerStatuses[*]}{.name}={.ready} {end}')"

STAMP_ID=$(api GET "/v1/accounts/$ACCOUNT_ID/stamps" \
    | "$CONTROL_PY" -c "import json,sys;print(json.load(sys.stdin)[0]['id'])")
info "stamp enrolled outbound: $STAMP_ID"

served() {
    "${KUBECTL[@]}" exec stamp-fabric-stamp-0 -c data-plane -- python -c "
import json, urllib.request
with urllib.request.urlopen('http://127.0.0.1:8081/readyz') as response:
    print(json.load(response)['deployments'])" 2>/dev/null
}

[ "$(served)" = "0" ] || die "the stamp served a deployment before any placement"
info "no placement yet, so nothing is served"

# --------------------------------------------------------------------------
log "5. Placement reaches a running pod"
# --------------------------------------------------------------------------

api POST "/v1/accounts/$ACCOUNT_ID/deployments/$DEPLOYMENT_ID/placements" \
    "{\"stamp_id\":\"$STAMP_ID\"}" >/dev/null

for _ in $(seq 1 40); do
    [ "$(served)" = "1" ] && break
    sleep 2
done
# No restart: the data plane follows the file the agent rewrites, which it did not
# do until this was found in a cluster.
[ "$(served)" = "1" ] || die "the placement never reached the running data plane"
info "the running data plane picked up the placement without a restart"

if [ "$OPERATOR" = "1" ]; then
    # --------------------------------------------------------------------------
    log "5b. The declaration and the operator's verdict"
    # --------------------------------------------------------------------------

    DECLARED=$("${KUBECTL[@]}" get fabricmodeldeployments -o json \
        | "$CONTROL_PY" -c "import json,sys;print(len(json.load(sys.stdin)['items']))")
    [ "$DECLARED" = "1" ] || die "expected one declared deployment, found $DECLARED"
    info "the agent declared a FabricModelDeployment"

    # The operator writes the ConfigMap; the agent has no permission to.
    RENDERED_BY=$("${KUBECTL[@]}" get configmap fabric-deployments \
        -o jsonpath='{.metadata.labels.app\.kubernetes\.io/managed-by}')
    [ "$RENDERED_BY" = "fabric-operator" ] \
        || die "configuration was not rendered by the operator (managed-by=$RENDERED_BY)"
    info "the operator rendered the data plane's ConfigMap"

    for _ in $(seq 1 40); do
        APPLIED=$("${KUBECTL[@]}" get fabricmodeldeployments -o jsonpath=\
'{.items[0].status.conditions[?(@.type=="Applied")].status}' 2>/dev/null || true)
        [ "$APPLIED" = "True" ] && break
        sleep 2
    done
    [ "$APPLIED" = "True" ] || die "the operator never reported Applied"
    REASON=$("${KUBECTL[@]}" get fabricmodeldeployments -o jsonpath=\
'{.items[0].status.conditions[?(@.type=="Applied")].reason}')
    info "operator status: Applied=True reason=$REASON"
fi

# --------------------------------------------------------------------------
log "6. Inference through the Service"
# --------------------------------------------------------------------------

INFERENCE_TOKEN=$(token_for fabric-inference)
SERVICE="http://stamp-fabric-stamp.default.svc"

# Runs curl in the cluster and returns its output.
#
# Deliberately not "kubectl run -i": attaching races the container starting, and on
# a lost race kubectl falls back to streaming logs and can return nothing, which
# made this check pass or fail depending on timing. Waiting for completion and
# reading the log is deterministic.
probe() {
    local name="$1"; shift
    "${KUBECTL[@]}" delete pod "$name" --ignore-not-found --wait=true >/dev/null 2>&1
    "${KUBECTL[@]}" run "$name" --restart=Never --image=curlimages/curl:8.11.1 \
        --quiet -- "$@" >/dev/null 2>&1
    "${KUBECTL[@]}" wait --for=jsonpath='{.status.phase}'=Succeeded "pod/$name" \
        --timeout=120s >/dev/null 2>&1 || true
    "${KUBECTL[@]}" logs "$name" 2>/dev/null
    "${KUBECTL[@]}" delete pod "$name" --wait=false >/dev/null 2>&1
}

probe models -sS "$SERVICE/v1/models" -H "Authorization: Bearer $INFERENCE_TOKEN" \
    | "$CONTROL_PY" -c "
import json, sys
names = [row['id'] for row in json.load(sys.stdin)['data']]
assert names == ['kind-model'], names
print(f'     models visible to the account: {names}')"

probe chat -sS -X POST "$SERVICE/v1/chat/completions" \
    -H "Authorization: Bearer $INFERENCE_TOKEN" -H 'Content-Type: application/json' \
    -d '{"model":"kind-model","messages":[{"role":"user","content":"hi"}]}' \
    | "$CONTROL_PY" -c "
import json, sys
body = json.load(sys.stdin)
assert body['model'] == 'kind-model', body
print(f\"     inference served: model={body['model']}\")"

# The code is tagged in the body rather than relying on curl's bare write-out,
# which does not survive the pod's log capture reliably.
REFUSED=$(probe forged -sS -o /dev/null -w 'HTTP_STATUS:%{http_code}' -X POST \
    "$SERVICE/v1/chat/completions" -H 'Authorization: Bearer forged.token.value' \
    -H 'Content-Type: application/json' -d '{"model":"kind-model","messages":[]}' \
    | grep -o 'HTTP_STATUS:[0-9]*' | cut -d: -f2 | tail -1)
[ "$REFUSED" = "401" ] || die "a forged token was not refused (got '$REFUSED')"
info "a token the control plane did not sign is refused: 401"

# --------------------------------------------------------------------------
log "7. Usage reaches the control plane from inside the cluster"
# --------------------------------------------------------------------------

usage_events() {
    api GET "/v1/accounts/$ACCOUNT_ID/deployments/$DEPLOYMENT_ID/usage" \
        | "$CONTROL_PY" -c "import json,sys;print(json.load(sys.stdin)['events'])"
}

for _ in $(seq 1 40); do
    [ "$(usage_events)" != "0" ] && break
    sleep 2
done

api GET "/v1/accounts/$ACCOUNT_ID/deployments/$DEPLOYMENT_ID/usage" \
    | "$CONTROL_PY" -c "
import json, sys
body = json.load(sys.stdin)
assert body['events'] >= 1, body
# The token counts came from the model host, through the data plane's buffer, the
# collector, and the control plane's ownership checks.
assert body['input_tokens'] >= 13, body
print(f\"     usage: events={body['events']} input={body['input_tokens']} \"
      f\"output={body['output_tokens']} stamp={body['stamps'][0]['stamp_id'][:8]}\")"

# The reason must name whoever actually applied the configuration: the operator when
# one runs, otherwise the agent itself.
if [ "$OPERATOR" = "1" ]; then
    EXPECTED_REASON="DataPlaneConfigurationRendered"
else
    EXPECTED_REASON="AgentAppliedLocalConfiguration"
fi

# In a file rather than a heredoc: a heredoc is stdin, so it would displace the piped
# response the checker is meant to read.
cat > "$WORK/check_status.py" <<'PYSTATUS'
import json
import sys

expected = sys.argv[1]
rows = json.load(sys.stdin)
assert rows, "the agent reported no status"
row = rows[0]
assert row["phase"] == "ready", row
reason = row["conditions"][0]["reason"] if row.get("conditions") else None
# The control plane must learn what the cluster did, not what the agent asked for.
assert reason == expected, f"reason={reason} expected={expected}"
print(f"     status: phase={row['phase']} generation={row['observed_generation']} "
      f"reason={reason}")
PYSTATUS

api GET "/v1/accounts/$ACCOUNT_ID/deployments/$DEPLOYMENT_ID/status" \
    | "$CONTROL_PY" "$WORK/check_status.py" "$EXPECTED_REASON"

# --------------------------------------------------------------------------
log "8. A restarted pod recovers on its own"
# --------------------------------------------------------------------------

# The rendered configuration is on an ephemeral volume while credentials are not.
# An agent that trusted its acknowledged generation would ask only for changes,
# receive nothing, and serve nothing for as long as it ran.
"${KUBECTL[@]}" delete pod stamp-fabric-stamp-0 --wait=false >/dev/null
sleep 5
"${KUBECTL[@]}" wait --for=condition=ready pod/stamp-fabric-stamp-0 --timeout=240s >/dev/null

for _ in $(seq 1 40); do
    [ "$(served)" = "1" ] && break
    sleep 2
done
[ "$(served)" = "1" ] || die "a restarted stamp did not rebuild its configuration"
info "the restarted stamp rebuilt its configuration from full desired state"

ENROLLMENTS=$(api GET "/v1/accounts/$ACCOUNT_ID/stamps" \
    | "$CONTROL_PY" -c "import json,sys;print(len(json.load(sys.stdin)))")
[ "$ENROLLMENTS" = "1" ] || die "the restart enrolled a second stamp ($ENROLLMENTS present)"
info "no second enrollment: the single-use token was not needed again"

log "Packaging verified"
cat <<EOF

  images          run as uid 65532 with a read-only root filesystem
  chart           values reach the containers, and probes are reachable by the
                  kubelet without exposing the administrative listener
  agent           enrolled outbound, rendered configuration for the data plane,
                  reported status, handed the collector its own credential
  data plane      followed the rendered file while running, served only the
                  owning account, refused an unsigned token
  collector       drained the pod-local administrative listener and forwarded
                  usage under a write-only credential
  restart         recovered without a new enrollment token

EOF
