#!/usr/bin/env bash
#
# The edge gateway (ADR 0021) in front of two stamps, locally, with no GPU or cluster.
#
#   control plane   real FastAPI process, SQLite, real HTTP
#   stamp-a         Go agent (region us-east) + data-plane gateway over HTTPS
#   stamp-b         Go agent (region eu-west) + data-plane gateway over HTTPS
#   edge            python -m fabric_data_plane.edge, the one URL callers use
#
# The model hosts are the only stubs: each gateway answers from an in-process host that
# says which stamp it is. The gateways serve TLS with a throwaway CA so the edge verifies
# them exactly as it would real stamps; stamp names resolve to 127.0.0.1 through the edge's
# address overrides, which keep the certificate check on the reported name.
#
# It shows: one URL serving a model placed on stamp A and another on stamp B, failover when
# stamp A stops, and serving continuing after the control plane stops, including an edge
# restarted during the outage from its cached routing table.
#
#   make demo-edge
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WORK="$(mktemp -d)"
# Everything here is local. A developer machine or CI runner with an HTTP(S) proxy
# configured would otherwise send the loopback and demo-stamp traffic through it.
export NO_PROXY="127.0.0.1,localhost,.edge-demo.test${NO_PROXY:+,$NO_PROXY}"
export no_proxy="$NO_PROXY"
PIDS=()

log()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
info() { printf '   %s\n' "$*"; }
die()  { printf '\033[31mERROR: %s\033[0m\n' "$*" >&2; for f in "$WORK"/*.log; do echo "--- $f"; tail -20 "$f"; done; exit 1; }

stop_pid() {
    local pid="$1"
    kill "$pid" 2>/dev/null || true
    for _ in $(seq 1 30); do kill -0 "$pid" 2>/dev/null || return 0; sleep 0.1; done
    kill -9 "$pid" 2>/dev/null || true
}
cleanup() {
    for pid in "${PIDS[@]:-}"; do [ -n "$pid" ] && stop_pid "$pid"; done
    rm -rf "$WORK"
}
trap cleanup EXIT

CONTROL_PY="$REPO_ROOT/control-plane/.venv/bin/python"
DATA_PY="$REPO_ROOT/data-plane/.venv/bin/python"
[ -x "$CONTROL_PY" ] || die "control-plane environment missing (make setup-python)"
[ -x "$DATA_PY" ] || die "data-plane environment missing (make setup-python)"

free_port() { "$CONTROL_PY" -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()'; }
wait_for() {  # url [curl args...]
    local url="$1"; shift
    for _ in $(seq 1 100); do curl -fsS "$@" "$url" >/dev/null 2>&1 && return 0; sleep 0.1; done
    return 1
}
json() { "$CONTROL_PY" -c "import json,sys;d=json.load(sys.stdin);print($1)"; }

PORT=$(free_port); PORT_A=$(free_port); PORT_B=$(free_port); EDGE_PORT=$(free_port); EDGE_ADMIN=$(free_port)
CONTROL_URL="http://127.0.0.1:$PORT"
EDGE_URL="http://127.0.0.1:$EDGE_PORT"

export FABRIC_APP_ENV=test
export FABRIC_DATABASE_URL="sqlite+aiosqlite:///$WORK/cp.db"
export FABRIC_JWT_ISSUER="http://control.edge-demo.test"
export FABRIC_AUTH0_AUDIENCE="unused-in-this-path"
export FABRIC_CREDENTIAL_PEPPER="edge-demo-pepper-not-a-secret"
export FABRIC_JWT_PRIVATE_KEY_PATH="$WORK/signing.pem"

# --------------------------------------------------------------------------
log "1. A throwaway CA and one certificate for both stamp gateways"
# --------------------------------------------------------------------------
openssl genrsa -out "$WORK/signing.pem" 2048 2>/dev/null
openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj "/CN=edge-demo CA" \
    -keyout "$WORK/ca.key" -out "$WORK/ca.pem" 2>/dev/null
openssl req -newkey rsa:2048 -nodes -subj "/CN=stamps.edge-demo.test" \
    -keyout "$WORK/stamp.key" -out "$WORK/stamp.csr" 2>/dev/null
printf 'subjectAltName=DNS:stamp-a.edge-demo.test,DNS:stamp-b.edge-demo.test\nbasicConstraints=CA:FALSE\n' \
    > "$WORK/san.ext"
openssl x509 -req -in "$WORK/stamp.csr" -CA "$WORK/ca.pem" -CAkey "$WORK/ca.key" \
    -CAcreateserial -days 1 -extfile "$WORK/san.ext" -out "$WORK/stamp.pem" 2>/dev/null
info "stamp-a.edge-demo.test and stamp-b.edge-demo.test, signed by a CA only the edge trusts"

# --------------------------------------------------------------------------
log "2. Control plane: migrate, seed an account, mint the edge credential, serve"
# --------------------------------------------------------------------------
cd "$REPO_ROOT/control-plane"
"$CONTROL_PY" -m alembic upgrade head >/dev/null 2>&1
"$CONTROL_PY" -m app.cli bootstrap-account --slug demo --email ops@demo.test > "$WORK/account.txt"
ACCOUNT_ID=$(awk '/^account/{print $2}' "$WORK/account.txt")
API_KEY=$(awk '/^api_key/{print $2}' "$WORK/account.txt")
"$CONTROL_PY" -m app.cli create-edge-credential --name demo-edge > "$WORK/edge-cred.txt"
awk '/^api_key/{print $2}' "$WORK/edge-cred.txt" > "$WORK/edge-key"
chmod 600 "$WORK/edge-key"
info "account $ACCOUNT_ID"
info "edge credential $(awk '/^key_id/{print $2, $3}' "$WORK/edge-cred.txt") (printed once, stored as a verifier)"
"$CONTROL_PY" -m uvicorn app.main:app --host 127.0.0.1 --port "$PORT" --log-level warning \
    >"$WORK/control-plane.log" 2>&1 &
CP_PID=$!; PIDS+=("$CP_PID")
cd "$REPO_ROOT"
wait_for "$CONTROL_URL/healthz" || die "control plane did not start"
info "control plane on $CONTROL_URL"

token() {
    curl -fsS -X POST "$CONTROL_URL/v1/token" -H 'Content-Type: application/json' \
        -d "{\"grant_type\":\"api_key\",\"api_key\":\"$API_KEY\",\"audience\":\"$1\"}" | json "d['access_token']"
}
CONTROL_TOKEN=$(token fabric-control)
INFERENCE_TOKEN=$(token fabric-inference)
curl -fsS "$CONTROL_URL/.well-known/jwks.json" > "$WORK/jwks.json"
api() {
    curl -fsS -X "$1" "$CONTROL_URL$2" -H "Authorization: Bearer $CONTROL_TOKEN" \
        -H 'Content-Type: application/json' ${3:+-d "$3"}
}

# --------------------------------------------------------------------------
log "3. Two stamps: enroll agents in different regions, place three deployments"
# --------------------------------------------------------------------------
(cd "$REPO_ROOT/agent" && go build -o "$WORK/fabric-agent" ./cmd/fabric-agent)
deployment() {
    api POST "/v1/accounts/$ACCOUNT_ID/deployments" \
        "{\"name\":\"$1\",\"model_alias\":\"$1\",\"spec\":{\"runtime\":{\"release\":\"demo-release\"}}}" \
        | json "d['id']"
}
DEP_A=$(deployment model-a); DEP_B=$(deployment model-b); DEP_SHARED=$(deployment model-shared)

agent() {  # name region port [enrollment token]
    FABRIC_AGENT_ENROLLMENT_TOKEN="${4:-}" "$WORK/fabric-agent" \
        --control-plane "$CONTROL_URL" --state-dir "$WORK/$1" --stamp-name "$1" \
        --upstream "http://model-host.$1.local" --orchestrator k3s --region "$2" \
        --inference-url "https://$1.edge-demo.test:$3" \
        --measure-capacity=false --gpus 4 --once >"$WORK/agent-$1.log" 2>&1 \
        || die "agent $1 failed"
}
for spec in "stamp-a us-east $PORT_A" "stamp-b eu-west $PORT_B"; do
    set -- $spec
    ENROLL=$(api POST "/v1/accounts/$ACCOUNT_ID/stamp-enrollment-tokens" \
        '{"allowed_mode":"byoi","expires_in_minutes":30}' | json "d['enrollment_token']")
    agent "$1" "$2" "$3" "$ENROLL"
done
STAMP_A=$(json "d['stamp_id']" < "$WORK/stamp-a/credentials.json")
STAMP_B=$(json "d['stamp_id']" < "$WORK/stamp-b/credentials.json")
place() { api POST "/v1/accounts/$ACCOUNT_ID/deployments/$1/placements" "{\"stamp_id\":\"$2\"}" >/dev/null; }
place "$DEP_A" "$STAMP_A"; place "$DEP_SHARED" "$STAMP_A"
place "$DEP_B" "$STAMP_B"; place "$DEP_SHARED" "$STAMP_B"
agent stamp-a us-east "$PORT_A"; agent stamp-b eu-west "$PORT_B"
info "stamp-a (us-east): model-a, model-shared"
info "stamp-b (eu-west): model-b, model-shared"

# --------------------------------------------------------------------------
log "4. Stamp gateways over HTTPS, each serving the file its agent wrote"
# --------------------------------------------------------------------------
cat > "$WORK/gateway.py" <<'PY'
import asyncio, json, os
import httpx, uvicorn
from fabric_data_plane.app import DataPlane, create_inference_app
from fabric_data_plane.config import Settings
from fabric_data_plane.keys import KeyCache
from fabric_data_plane.registry import DeploymentRegistry

name = os.environ["STAMP"]
jwks = os.environ["JWKS"]
settings = Settings(app_env="test", jwt_issuer=os.environ["ISSUER"],
                    jwks_url=os.environ["ISSUER"] + "/.well-known/jwks.json",
                    jwks_file=jwks, deployments_file=os.environ["DEPLOYMENTS"])

def host(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    return httpx.Response(200, json={
        "id": "cmpl-demo", "object": "chat.completion", "model": body["model"],
        "choices": [{"index": 0, "message": {"role": "assistant",
                     "content": f"hello from {name}"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 3},
    })

plane = DataPlane(
    settings=settings,
    keys=KeyCache(settings, client=httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json=json.load(open(jwks)))))),
    registry=DeploymentRegistry.load(os.environ["DEPLOYMENTS"]),
    client=httpx.AsyncClient(transport=httpx.MockTransport(host)),
)
asyncio.run(uvicorn.Server(uvicorn.Config(
    create_inference_app(plane), host="127.0.0.1", port=int(os.environ["PORT"]),
    ssl_certfile=os.environ["CERT"], ssl_keyfile=os.environ["KEY"], log_level="warning",
)).serve())
PY
gateway() {  # name port
    (cd "$REPO_ROOT/data-plane" && STAMP="$1" PORT="$2" JWKS="$WORK/jwks.json" \
        ISSUER="$FABRIC_JWT_ISSUER" DEPLOYMENTS="$WORK/$1/deployments.json" \
        CERT="$WORK/stamp.pem" KEY="$WORK/stamp.key" \
        exec "$DATA_PY" "$WORK/gateway.py") >"$WORK/gateway-$1.log" 2>&1 &
    echo $!
}
GW_A=$(gateway stamp-a "$PORT_A"); PIDS+=("$GW_A")
GW_B=$(gateway stamp-b "$PORT_B"); PIDS+=("$GW_B")
wait_for "https://127.0.0.1:$PORT_A/healthz" --cacert "$WORK/ca.pem" \
    --resolve "stamp-a.edge-demo.test:$PORT_A:127.0.0.1" \
    || wait_for "https://stamp-a.edge-demo.test:$PORT_A/healthz" --cacert "$WORK/ca.pem" \
        --resolve "stamp-a.edge-demo.test:$PORT_A:127.0.0.1" || die "stamp-a gateway did not start"
wait_for "https://stamp-b.edge-demo.test:$PORT_B/healthz" --cacert "$WORK/ca.pem" \
    --resolve "stamp-b.edge-demo.test:$PORT_B:127.0.0.1" || die "stamp-b gateway did not start"
info "stamp-a https://stamp-a.edge-demo.test:$PORT_A  stamp-b https://stamp-b.edge-demo.test:$PORT_B"

# --------------------------------------------------------------------------
log "5. The edge: one URL for every stamp"
# --------------------------------------------------------------------------
start_edge() {
    (cd "$REPO_ROOT/data-plane" && \
        FABRIC_EDGE_PORT="$EDGE_PORT" FABRIC_EDGE_ADMIN_PORT="$EDGE_ADMIN" \
        FABRIC_EDGE_CONTROL_PLANE_URL="$CONTROL_URL" FABRIC_EDGE_API_KEY_FILE="$WORK/edge-key" \
        FABRIC_EDGE_JWT_ISSUER="$FABRIC_JWT_ISSUER" \
        FABRIC_EDGE_JWKS_URL="$CONTROL_URL/.well-known/jwks.json" \
        FABRIC_EDGE_JWKS_FILE="$WORK/jwks.json" \
        FABRIC_EDGE_TABLE_CACHE_PATH="$WORK/edge/routing-table.json" \
        FABRIC_EDGE_STAMP_CA_BUNDLE="$WORK/ca.pem" \
        FABRIC_EDGE_STAMP_ADDRESS_OVERRIDES="{\"stamp-a.edge-demo.test:$PORT_A\":\"127.0.0.1:$PORT_A\",\"stamp-b.edge-demo.test:$PORT_B\":\"127.0.0.1:$PORT_B\"}" \
        FABRIC_EDGE_POLL_INTERVAL_SECONDS=1 FABRIC_EDGE_DEFAULT_REGION=us-east \
        FABRIC_EDGE_STAMP_FAILURE_THRESHOLD=1 FABRIC_EDGE_LOG_LEVEL=warning \
        exec "$DATA_PY" -m fabric_data_plane.edge) >>"$WORK/edge.log" 2>&1 &
    echo $!
}
EDGE_PID=$(start_edge); PIDS+=("$EDGE_PID")
wait_for "http://127.0.0.1:$EDGE_ADMIN/readyz" || die "edge did not become ready"
curl -fsS "http://127.0.0.1:$EDGE_ADMIN/admin/routing" | json "
'   revision %s from %s: %s deployments on %s' % (d['table']['revision'], d['source'],
 d['table']['deployments'], ', '.join('%s(%s, %s placements)' % (s['name'], s['region'], s['placements']) for s in d['table']['stamps']))"
info "edge on $EDGE_URL"

ask() {  # model -> "status stamp reply"
    local out
    out=$(curl -sS -o "$WORK/reply.json" -D "$WORK/headers.txt" -w '%{http_code}' \
        -X POST "$EDGE_URL/v1/chat/completions" \
        -H "Authorization: Bearer $INFERENCE_TOKEN" -H 'Content-Type: application/json' \
        -d "{\"model\":\"$1\",\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}]}")
    local stamp
    stamp=$(awk -F': ' 'tolower($1)=="x-fabric-edge-stamp"{print $2}' "$WORK/headers.txt" | tr -d '\r')
    local reply
    reply=$("$CONTROL_PY" -c "
import json;d=json.load(open('$WORK/reply.json'))
print(d['choices'][0]['message']['content'] if 'choices' in d else d.get('error',{}).get('code',d))")
    printf '%s %s %s' "$out" "${stamp:--}" "$reply"
}
expect() {  # model status stamp
    local got; got=$(ask "$1")
    info "model=$1 -> $got"
    set -- "$1" "$2" "$3" $got
    [ "$4" = "$2" ] || die "expected HTTP $2 for $1, got $4"
    [ "$3" = "-" ] || [ "$5" = "$3" ] || die "expected $1 on $3, got $5"
}

MODELS=$(curl -fsS "$EDGE_URL/v1/models" -H "Authorization: Bearer $INFERENCE_TOKEN" | json "[m['id'] for m in d['data']]")
info "GET /v1/models (from the table, no stamp contacted): $MODELS"
expect model-a 200 stamp-a
expect model-b 200 stamp-b
expect model-shared 200 stamp-a
BAD=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$EDGE_URL/v1/chat/completions" \
    -H 'Authorization: Bearer not-a-token' -d '{"model":"model-a"}')
[ "$BAD" = "401" ] || die "a forged token got $BAD"
info "forged token -> $BAD at the edge, no stamp contacted"

# --------------------------------------------------------------------------
log "6. Stop stamp-a's gateway: model-shared fails over to stamp-b"
# --------------------------------------------------------------------------
stop_pid "$GW_A"
expect model-shared 200 stamp-b
expect model-shared 200 stamp-b
expect model-a 503 -
curl -fsS "http://127.0.0.1:$EDGE_ADMIN/metrics" | grep -E '^fabric_edge_(stamp_ejections|retries)_total' | sed 's/^/     /'

# --------------------------------------------------------------------------
log "7. Stop the control plane: the edge keeps serving"
# --------------------------------------------------------------------------
stop_pid "$CP_PID"
curl -fsS --max-time 1 "$CONTROL_URL/healthz" >/dev/null 2>&1 && die "control plane still up"
info "control plane stopped"
sleep 2
expect model-b 200 stamp-b
expect model-shared 200 stamp-b
curl -fsS "http://127.0.0.1:$EDGE_ADMIN/admin/routing" | json "'   last poll error: %s' % d['last_poll_error']"

log "8. Restart the edge during the outage: it serves from its cached table"
stop_pid "$EDGE_PID"
EDGE_PID=$(start_edge); PIDS+=("$EDGE_PID")
wait_for "http://127.0.0.1:$EDGE_ADMIN/readyz" || die "restarted edge did not become ready"
curl -fsS "http://127.0.0.1:$EDGE_ADMIN/admin/routing" | json "'   source=%s revision=%s age=%.1fs' % (d['source'], d['table']['revision'], d['age_seconds'])"
expect model-b 200 stamp-b
expect model-shared 200 stamp-b

log "Edge demo complete"
cat <<EOF

  one URL ($EDGE_URL) served model-a from stamp-a and model-b from stamp-b,
  failed model-shared over to stamp-b when stamp-a stopped, and kept serving with the
  control plane down, including after a restart from the cached routing table.
EOF
