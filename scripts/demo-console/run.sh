#!/usr/bin/env bash
#
# Run the Fabric console locally against a real control plane with a seeded demo account.
# `make demo-console` calls this. See docs/console-demo.md.
#
#   identity provider  MOCK  scripts/demo-console/mock_oidc.py (HTTPS, throwaway CA)
#   control plane      real  FastAPI + SQLite, migrated from scratch
#   stamps (3)         real  Go agent enrollment, desired state, credentials
#   data planes (3)    real  one per stamp, configured from the agent-written file
#   collector          real  forwards usage from each data plane
#   edge gateway       real  one HTTPS inference URL in front of the three stamps
#   model host         MOCK  scripts/demo-console/model_host.py (canned, streamed answers)
#   console            real  v1/, unmodified (production build by default)
#
# Everything generated (CA, keys, database, secrets) lives in a temporary directory that is
# deleted on exit. Nothing is written into the repository.
#
# Environment:
#   DEMO_CONSOLE_PORT    console port (default 3000)
#   DEMO_CONTROL_PORT    control-plane port (default 8080)
#   DEMO_IDP_PORT        mock identity provider HTTPS port (default 8443)
#   DEMO_IDP_BROWSER_PORT  its plain-HTTP localhost port for /authorize and logout (8444)
#   DEMO_INFERENCE_PORT  aks-gpu data plane HTTPS port (default 8090)
#   DEMO_EDGE_PORT       edge gateway HTTPS port, used by the playground (default 8091)
#   DEMO_MODEL_PORT      mock model host port (default 9009)
#   DEMO_CONSOLE_MODE    "start" (default): next build + next start; "dev": next dev
#   DEMO_SCREENSHOTS=1   capture docs/screenshots/console/ and exit instead of waiting
#   DEMO_KEEP_WORK=1     keep the temporary directory (logs) after exit

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
HERE="$REPO_ROOT/scripts/demo-console"
CONSOLE_PORT="${DEMO_CONSOLE_PORT:-3000}"
CONTROL_PORT="${DEMO_CONTROL_PORT:-8080}"
IDP_PORT="${DEMO_IDP_PORT:-8443}"
IDP_BROWSER_PORT="${DEMO_IDP_BROWSER_PORT:-8444}"
# The Auth0 SDK takes a bare host name (no port, localhost or IP), so the mock provider has
# a name of its own. Nothing resolves it: resolve-idp.cjs (Node) and pyshim (Python) send
# connections for it to 127.0.0.1:$IDP_PORT, verified against the throwaway CA.
IDP_HOST="idp.fabric-demo.test"
INFERENCE_PORT="${DEMO_INFERENCE_PORT:-8090}"
EDGE_PORT="${DEMO_EDGE_PORT:-8091}"
# The edge gateway's public name. Like the identity provider's, nothing resolves it: the
# console's resolve-idp.cjs sends it to 127.0.0.1:$EDGE_PORT, verified against the demo CA.
EDGE_HOST="inference.northstar-labs.example"
MODEL_PORT="${DEMO_MODEL_PORT:-9009}"
CONSOLE_MODE="${DEMO_CONSOLE_MODE:-start}"
AUDIENCE="https://api.fabric.demo/control"
JWT_ISSUER="https://control.fabric.demo"

CONTROL_PY="$REPO_ROOT/control-plane/.venv/bin/python"
DATA_PY="$REPO_ROOT/data-plane/.venv/bin/python"
WORK="$(mktemp -d -t fabric-demo-console.XXXXXX)"
PIDS=()

log()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
info() { printf '   %s\n' "$*"; }
die()  { printf '\033[31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

cleanup() {
    local status=$?
    trap - EXIT INT TERM
    for (( i=${#PIDS[@]}-1; i>=0; i-- )); do
        kill "${PIDS[$i]}" 2>/dev/null || true
    done
    for pid in "${PIDS[@]}"; do
        for _ in $(seq 1 50); do kill -0 "$pid" 2>/dev/null || break; sleep 0.1; done
        kill -9 "$pid" 2>/dev/null || true
    done
    # next dev forks workers; make sure none outlive the demo.
    pkill -f "$WORK" 2>/dev/null || true
    if [ "${DEMO_KEEP_WORK:-}" = "1" ] || [ "$status" -ne 0 ]; then
        printf '   logs kept in %s\n' "$WORK"
    else
        rm -rf "$WORK"
    fi
    exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT TERM

[ -x "$CONTROL_PY" ] || die "control-plane environment missing (run: make setup-python)"
[ -x "$DATA_PY" ] || die "data-plane environment missing (run: make setup-python)"
[ -d "$REPO_ROOT/v1/node_modules" ] || die "console dependencies missing (run: cd v1 && npm ci)"
command -v go >/dev/null || die "go is required to build the agent and collector"

for port in "$CONSOLE_PORT" "$CONTROL_PORT" "$IDP_PORT" "$IDP_BROWSER_PORT" "$INFERENCE_PORT" "$EDGE_PORT" "$MODEL_PORT"; do
    if (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null; then
        die "port $port is already in use; set the DEMO_*_PORT variables to free ones"
    fi
done

wait_for() {  # url, name, [curl options]
    local url="$1" name="$2"; shift 2
    for _ in $(seq 1 300); do
        if curl -fsS -o /dev/null --max-time 2 "$@" "$url" 2>/dev/null; then return 0; fi
        sleep 0.2
    done
    die "$name did not start (logs in $WORK)"
}

export NO_PROXY="localhost,127.0.0.1,$IDP_HOST${NO_PROXY:+,$NO_PROXY}"
export no_proxy="$NO_PROXY"

# --------------------------------------------------------------------------
log "1. Throwaway CA and secrets"
# --------------------------------------------------------------------------
openssl req -x509 -newkey rsa:2048 -nodes -days 2 -subj "/CN=Fabric demo CA (temporary)" \
    -keyout "$WORK/ca-key.pem" -out "$WORK/ca.pem" \
    -addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,keyCertSign" \
    >/dev/null 2>&1
openssl req -newkey rsa:2048 -nodes -subj "/CN=localhost" \
    -keyout "$WORK/server-key.pem" -out "$WORK/server.csr" >/dev/null 2>&1
# One certificate for every demo listener: the identity provider, each stamp's reported
# gateway name (verified by the edge gateway) and the edge gateway's public name.
printf 'subjectAltName=DNS:localhost,DNS:%s,DNS:*.gw.northstar-labs.example,DNS:%s,IP:127.0.0.1\nextendedKeyUsage=serverAuth\n' \
    "$IDP_HOST" "$EDGE_HOST" > "$WORK/server.ext"
openssl x509 -req -in "$WORK/server.csr" -CA "$WORK/ca.pem" -CAkey "$WORK/ca-key.pem" \
    -CAcreateserial -days 2 -extfile "$WORK/server.ext" -out "$WORK/server.pem" >/dev/null 2>&1
openssl genrsa -out "$WORK/signing.pem" 2048 2>/dev/null
CLIENT_ID="demo-console-$(openssl rand -hex 6)"
CLIENT_SECRET="$(openssl rand -hex 32)"
SESSION_SECRET="$(openssl rand -hex 32)"
info "generated in $WORK (deleted on exit)"

# --------------------------------------------------------------------------
log "2. Build the agent and collector"
# --------------------------------------------------------------------------
(cd "$REPO_ROOT/agent" && go build -o "$WORK/fabric-agent" ./cmd/fabric-agent \
    && go build -o "$WORK/fabric-collector" ./cmd/fabric-collector)
info "built fabric-agent and fabric-collector"

# --------------------------------------------------------------------------
log "3. Mock identity provider (stands in for Auth0)"
# --------------------------------------------------------------------------
IDP_URL="https://localhost:$IDP_PORT"
export DEMO_IDP_HOST="$IDP_HOST" DEMO_IDP_ADDR="127.0.0.1:$IDP_PORT"
"$CONTROL_PY" "$HERE/mock_oidc.py" --port "$IDP_PORT" --browser-port "$IDP_BROWSER_PORT" \
    --public-host "$IDP_HOST" \
    --cert "$WORK/server.pem" --key "$WORK/server-key.pem" \
    --client-id "$CLIENT_ID" --client-secret "$CLIENT_SECRET" --audience "$AUDIENCE" \
    >"$WORK/idp.log" 2>&1 &
PIDS+=($!)
wait_for "$IDP_URL/.well-known/openid-configuration" "identity provider" --cacert "$WORK/ca.pem"
info "issuer https://$IDP_HOST/ on $IDP_URL; browser endpoints on http://localhost:$IDP_BROWSER_PORT"

# --------------------------------------------------------------------------
log "4. Control plane"
# --------------------------------------------------------------------------
CONTROL_URL="http://127.0.0.1:$CONTROL_PORT"
export FABRIC_APP_ENV=local
export FABRIC_DATABASE_URL="sqlite+aiosqlite:///$WORK/fabric.db"
export FABRIC_AUTH0_ISSUER="https://$IDP_HOST/"
export FABRIC_AUTH0_AUDIENCE="$AUDIENCE"
export FABRIC_JWT_ISSUER="$JWT_ISSUER"
export FABRIC_JWT_PRIVATE_KEY_PATH="$WORK/signing.pem"
export FABRIC_CREDENTIAL_PEPPER="demo-pepper-$(openssl rand -hex 16)"
(
    cd "$REPO_ROOT/control-plane"
    "$CONTROL_PY" -m alembic upgrade head >"$WORK/migrate.log" 2>&1
    "$CONTROL_PY" -m app.cli seed-system-account >>"$WORK/migrate.log" 2>&1
    # The edge gateway's routing-table credential (edge:routing:read only), as an operator
    # mints it for the Helm chart.
    "$CONTROL_PY" -m app.cli create-edge-credential --name fabric-edge >"$WORK/edge-credential.txt" 2>>"$WORK/migrate.log"
) || { cat "$WORK/migrate.log"; die "migration failed"; }
EDGE_API_KEY=$(awk '$1 == "api_key" {print $2}' "$WORK/edge-credential.txt")
[ -n "$EDGE_API_KEY" ] || die "could not create the edge gateway credential"
rm -f "$WORK/edge-credential.txt"
(
    cd "$REPO_ROOT/control-plane"
    # The control plane fetches the identity provider's keys over HTTPS; trust only the
    # throwaway CA for that, rather than turning verification off. The asyncio loop, because
    # uvloop resolves names itself and would bypass the pyshim name mapping.
    SSL_CERT_FILE="$WORK/ca.pem" PYTHONPATH="$HERE/pyshim" exec "$CONTROL_PY" -m uvicorn app.main:app \
        --host 127.0.0.1 --port "$CONTROL_PORT" --log-level warning --loop asyncio
) >"$WORK/control-plane.log" 2>&1 &
PIDS+=($!)
wait_for "$CONTROL_URL/healthz" "control plane"
info "$CONTROL_URL"

# --------------------------------------------------------------------------
log "5. Mock model host"
# --------------------------------------------------------------------------
"$CONTROL_PY" "$HERE/model_host.py" --port "$MODEL_PORT" >"$WORK/model-host.log" 2>&1 &
PIDS+=($!)
wait_for "http://127.0.0.1:$MODEL_PORT/health" "model host"
info "http://127.0.0.1:$MODEL_PORT"

# --------------------------------------------------------------------------
log "6. Seed Northstar Labs through the real APIs"
# --------------------------------------------------------------------------
STATE="$WORK/state.json"
"$CONTROL_PY" -u "$HERE/seed.py" --work "$WORK" --control-url "$CONTROL_URL" \
    --idp-url "$IDP_URL" --ca "$WORK/ca.pem" \
    --client-id "$CLIENT_ID" --client-secret "$CLIENT_SECRET" --audience "$AUDIENCE" \
    --jwt-issuer "$JWT_ISSUER" --agent-bin "$WORK/fabric-agent" \
    --collector-bin "$WORK/fabric-collector" --model-host-url "http://127.0.0.1:$MODEL_PORT" \
    --data-plane-python "$DATA_PY" --data-plane-dir "$REPO_ROOT/data-plane" \
    --inference-port "$INFERENCE_PORT" --state-file "$STATE" 2>&1 | tee "$WORK/seed.log" &
PIDS+=($!)
for _ in $(seq 1 900); do
    [ -f "$STATE" ] && break
    kill -0 "${PIDS[-1]}" 2>/dev/null || die "seeding failed (see above)"
    sleep 0.2
done
[ -f "$STATE" ] || die "seeding did not finish"

# --------------------------------------------------------------------------
log "6b. Edge gateway"
# --------------------------------------------------------------------------
EDGE_ADMIN_PORT=$("$CONTROL_PY" -c "import socket;s=socket.socket();s.bind(('127.0.0.1',0));print(s.getsockname()[1])")
EDGE_OVERRIDES=$("$CONTROL_PY" -c "import json;print(json.dumps(json.load(open('$STATE'))['gateway_addresses']))")
(
    cd "$REPO_ROOT/data-plane"
    export FABRIC_EDGE_HOST=127.0.0.1 FABRIC_EDGE_PORT="$EDGE_PORT"
    export FABRIC_EDGE_ADMIN_HOST=127.0.0.1 FABRIC_EDGE_ADMIN_PORT="$EDGE_ADMIN_PORT"
    export FABRIC_EDGE_CONTROL_PLANE_URL="$CONTROL_URL" FABRIC_EDGE_API_KEY="$EDGE_API_KEY"
    export FABRIC_EDGE_JWT_ISSUER="$JWT_ISSUER"
    export FABRIC_EDGE_JWKS_URL="$CONTROL_URL/.well-known/jwks.json"
    export FABRIC_EDGE_POLL_INTERVAL_SECONDS=2
    export FABRIC_EDGE_STAMP_CA_BUNDLE="$WORK/ca.pem"
    export FABRIC_EDGE_STAMP_ADDRESS_OVERRIDES="$EDGE_OVERRIDES"
    export DEMO_EDGE_CERT="$WORK/server.pem" DEMO_EDGE_KEY="$WORK/server-key.pem"
    exec "$DATA_PY" "$HERE/edge.py"
) >"$WORK/edge.log" 2>&1 &
PIDS+=($!)
wait_for "http://127.0.0.1:$EDGE_ADMIN_PORT/readyz" "edge gateway"
INFERENCE_URL="https://$EDGE_HOST"
info "$INFERENCE_URL on https://localhost:$EDGE_PORT, routing to every stamp's data plane"

# --------------------------------------------------------------------------
log "7. Console"
# --------------------------------------------------------------------------
CONSOLE_URL="http://localhost:$CONSOLE_PORT"
if [ "$CONSOLE_MODE" = "dev" ]; then
    info "next dev (hot reload)"
else
    info "next build, then next start (takes a few minutes; DEMO_CONSOLE_MODE=dev skips it)"
fi
(
    cd "$REPO_ROOT/v1"
    export APP_BASE_URL="$CONSOLE_URL"
    export AUTH0_DOMAIN="$IDP_HOST"
    export NODE_OPTIONS="--require $HERE/resolve-idp.cjs${NODE_OPTIONS:+ $NODE_OPTIONS}"
    export DEMO_HOST_MAP="$EDGE_HOST=127.0.0.1:$EDGE_PORT"
    export AUTH0_CLIENT_ID="$CLIENT_ID"
    export AUTH0_CLIENT_SECRET="$CLIENT_SECRET"
    export AUTH0_SECRET="$SESSION_SECRET"
    export AUTH0_AUDIENCE="$AUDIENCE"
    export FABRIC_CONTROL_PLANE_URL="$CONTROL_URL"
    export FABRIC_CONTROL_PLANE_PUBLIC_URL="$JWT_ISSUER"
    export FABRIC_CONTROL_PLANE_JWT_ISSUER="$JWT_ISSUER"
    # The edge reaches every stamp, so the playground works for deployments on any of them.
    export FABRIC_INFERENCE_URL="$INFERENCE_URL"
    export FABRIC_EDGE_INFERENCE_URL="$INFERENCE_URL"
    # Node trusts the throwaway CA in addition to its usual roots.
    export NODE_EXTRA_CA_CERTS="$WORK/ca.pem"
    export NEXT_TELEMETRY_DISABLED=1
    if [ "$CONSOLE_MODE" = "dev" ]; then
        exec node node_modules/next/dist/bin/next dev --port "$CONSOLE_PORT" --hostname localhost
    fi
    node node_modules/next/dist/bin/next build >&2
    exec node node_modules/next/dist/bin/next start --port "$CONSOLE_PORT" --hostname localhost
) >"$WORK/console.log" 2>&1 &
PIDS+=($!)
for _ in $(seq 1 600); do  # a production build takes a few minutes
    curl -fsS -o /dev/null --max-time 2 "$CONSOLE_URL/healthz" 2>/dev/null && break
    kill -0 "${PIDS[-1]}" 2>/dev/null || { tail -40 "$WORK/console.log"; die "console failed to start"; }
    sleep 1
done
curl -fsS -o /dev/null --max-time 5 "$CONSOLE_URL/healthz" || die "console did not start"
info "$CONSOLE_URL"

cat <<EOF

  Fabric console demo is running.

    Console          $CONSOLE_URL
    Sign in          click "Sign in"; the demo identity provider signs you in as
                     Maya Chen (maya.chen@northstar-labs.example), owner of Northstar Labs
    Control plane    $CONTROL_URL/docs
    Edge gateway     $INFERENCE_URL (https://localhost:$EDGE_PORT)
    Playground       answers come from a stub model host through the edge and the real data planes

  Logs: $WORK
EOF

if [ "${DEMO_SCREENSHOTS:-}" = "1" ]; then
    log "8. Screenshots"
    DEMO_CONSOLE_URL="$CONSOLE_URL" DEMO_STATE="$STATE" \
        DEMO_OUT="${DEMO_SCREENSHOT_DIR:-$REPO_ROOT/docs/screenshots/console}" \
        node "$HERE/screenshots.mjs"
    exit 0
fi

printf '\n  Press Ctrl-C to stop.\n'
wait "${PIDS[-1]}"
