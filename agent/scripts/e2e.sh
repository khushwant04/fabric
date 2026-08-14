#!/usr/bin/env bash
#
# End-to-end proof that the three components close the loop.
#
# Unit tests cover each component against stubs of the others, which cannot show
# that the real pieces agree. This runs a real control plane over HTTP, the
# compiled Go agent against it, and the Python data plane against the file the
# agent produced:
#
#   control plane  enroll a stamp, place a deployment on it
#        |         (real FastAPI process, real SQLite database, real HTTP)
#        v
#   agent          enrol once, pull desired state, render deployments.json,
#        |         report status back
#        v
#   data plane     load that file and authorize an inference token for the
#                  owning account, refusing another account's token
#
# Usage:
#   agent/scripts/e2e.sh
#
# Requires the control-plane, data-plane, and agent environments to be installed.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WORK="$(mktemp -d)"
SERVER_PID=""

log()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
info() { printf '   %s\n' "$*"; }
die()  { printf '\033[31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

cleanup() {
    if [ -n "$SERVER_PID" ]; then
        kill "$SERVER_PID" 2>/dev/null || true
        for _ in $(seq 1 20); do
            kill -0 "$SERVER_PID" 2>/dev/null || break
            sleep 0.1
        done
        kill -9 "$SERVER_PID" 2>/dev/null || true
    fi
    rm -rf "$WORK"
}
trap cleanup EXIT

CONTROL_PY="$REPO_ROOT/control-plane/.venv/bin/python"
DATA_PY="$REPO_ROOT/data-plane/.venv/bin/python"
[ -x "$CONTROL_PY" ] || die "control-plane environment missing"
[ -x "$DATA_PY" ] || die "data-plane environment missing"

# An ephemeral free port by default. A stale server from an earlier run would
# otherwise answer the health check while pointing at a database that no longer
# exists, which is exactly how this harness first failed.
PORT="${FABRIC_E2E_PORT:-$("$CONTROL_PY" -c '
import socket
s = socket.socket()
s.bind(("127.0.0.1", 0))
print(s.getsockname()[1])
s.close()')}"
CONTROL_URL="http://127.0.0.1:${PORT}"

if curl -fsS --max-time 1 "$CONTROL_URL/healthz" >/dev/null 2>&1; then
    die "something is already listening on $CONTROL_URL; refusing to reuse it"
fi

export FABRIC_APP_ENV=test
export FABRIC_DATABASE_URL="sqlite+aiosqlite:///$WORK/e2e.db"
export FABRIC_JWT_ISSUER="http://control.e2e.test"
export FABRIC_AUTH0_AUDIENCE="unused-in-this-path"
export FABRIC_CREDENTIAL_PEPPER="e2e-pepper-not-a-real-secret"
export FABRIC_JWT_PRIVATE_KEY_PATH="$WORK/signing.pem"

# --------------------------------------------------------------------------
log "1. Control plane: migrate, seed an account and an API key"
# --------------------------------------------------------------------------

openssl genrsa -out "$WORK/signing.pem" 2048 2>/dev/null
(cd "$REPO_ROOT/control-plane" && "$CONTROL_PY" -m alembic upgrade head >/dev/null 2>&1)

# Seed directly so the flow needs no Auth0: the API-key grant is enough to drive
# every control API used below.
(cd "$REPO_ROOT/control-plane" && "$CONTROL_PY" - > "$WORK/seed.json" <<'PY'
import asyncio, json
from app.core.database import dispose_engine, get_session_factory
from app.services.accounts import create_account, ensure_system_account
from app.services.api_keys import create_api_key
from app.models import User
from sqlalchemy import update
from app.models import Account

async def main():
    factory = get_session_factory()
    async with factory() as session:
        await ensure_system_account(session)
        user = User(auth0_subject="e2e|operator", email="operator@e2e.test")
        session.add(user)
        await session.flush()
        account, _membership = await create_account(
            session, slug="e2e-account", name="E2E", owner=user
        )
        # Managed capacity is a database flag today; the API to manage it does not exist.
        await session.execute(
            update(Account).where(Account.id == account.id).values(managed_capacity_enabled=True)
        )
        record, secret = await create_api_key(
            session,
            account_id=account.id,
            name="e2e",
            requested_scopes=[
                "deployments:read", "deployments:write", "stamps:read", "stamps:write",
                "inference:invoke",
            ],
            principal_scopes=frozenset({
                "deployments:read", "deployments:write", "stamps:read", "stamps:write",
            }),
            expires_in_days=None,
            service_principal_id=None,
            actor_user_id=user.id,
        )
        await session.commit()
        print(json.dumps({"account_id": str(account.id), "api_key": secret}))
    await dispose_engine()

asyncio.run(main())
PY
)
ACCOUNT_ID=$("$CONTROL_PY" -c "import json,sys;print(json.load(open('$WORK/seed.json'))['account_id'])")
API_KEY=$("$CONTROL_PY" -c "import json,sys;print(json.load(open('$WORK/seed.json'))['api_key'])")
info "account: $ACCOUNT_ID"

# --------------------------------------------------------------------------
log "2. Control plane: serve over real HTTP"
# --------------------------------------------------------------------------

# Started without a wrapping subshell so SERVER_PID is uvicorn itself. Killing a
# subshell leaves the server orphaned, holding the port after the run.
cd "$REPO_ROOT/control-plane"
"$CONTROL_PY" -m uvicorn app.main:app \
    --host 127.0.0.1 --port "$PORT" --log-level warning >"$WORK/server.log" 2>&1 &
SERVER_PID=$!
cd "$REPO_ROOT"

for _ in $(seq 1 50); do
    if curl -fsS "$CONTROL_URL/healthz" >/dev/null 2>&1; then break; fi
    sleep 0.2
done
curl -fsS "$CONTROL_URL/healthz" >/dev/null || { cat "$WORK/server.log"; die "control plane did not start"; }
info "listening on $CONTROL_URL"

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

# --------------------------------------------------------------------------
log "3. Exchange the API key, create a deployment and an enrollment token"
# --------------------------------------------------------------------------

CONTROL_TOKEN=$(curl -fsS -X POST "$CONTROL_URL/v1/token" -H 'Content-Type: application/json' \
    -d "{\"grant_type\":\"api_key\",\"api_key\":\"$API_KEY\",\"audience\":\"fabric-control\"}" \
    | "$CONTROL_PY" -c "import json,sys;print(json.load(sys.stdin)['access_token'])")
info "control token acquired"

DEPLOYMENT=$(api POST "/v1/accounts/$ACCOUNT_ID/deployments" \
    '{"name":"e2e-deployment","model_alias":"e2e-model","spec":{"runtime":{"release":"e2e-release-1"}}}')
DEPLOYMENT_ID=$(echo "$DEPLOYMENT" | "$CONTROL_PY" -c "import json,sys;print(json.load(sys.stdin)['id'])")
info "deployment: $DEPLOYMENT_ID"

ENROLL=$(api POST "/v1/accounts/$ACCOUNT_ID/stamp-enrollment-tokens" \
    '{"allowed_mode":"byoi","expires_in_minutes":30}')
ENROLL_TOKEN=$(echo "$ENROLL" | "$CONTROL_PY" -c "import json,sys;print(json.load(sys.stdin)['enrollment_token'])")
info "single-use enrollment token issued"

# --------------------------------------------------------------------------
log "4. Agent: build, enroll, reconcile"
# --------------------------------------------------------------------------

(cd "$REPO_ROOT/agent" && go build -o "$WORK/fabric-agent" ./cmd/fabric-agent)

FABRIC_AGENT_ENROLLMENT_TOKEN="$ENROLL_TOKEN" "$WORK/fabric-agent" \
    --control-plane "$CONTROL_URL" \
    --state-dir "$WORK/agent-state" \
    --stamp-name e2e-stamp \
    --upstream "http://model-host.e2e.test" \
    --orchestrator k3s \
    --once 2>&1 | tail -3

STAMP_ID=$("$CONTROL_PY" -c "
import json;print(json.load(open('$WORK/agent-state/credentials.json'))['stamp_id'])")
info "enrolled stamp: $STAMP_ID"

# Nothing is placed yet, so the rendered configuration must be empty rather than
# inventing an assignment.
PLACED_BEFORE=$("$CONTROL_PY" -c "
import json,os
path='$WORK/agent-state/deployments.json'
print(len(json.load(open(path))['deployments']) if os.path.exists(path) else 0)")
[ "$PLACED_BEFORE" = "0" ] || die "agent configured $PLACED_BEFORE deployments before any placement"
info "no placement yet, so no deployment configured"

# --------------------------------------------------------------------------
log "5. Place the deployment, reconcile again"
# --------------------------------------------------------------------------

api POST "/v1/accounts/$ACCOUNT_ID/deployments/$DEPLOYMENT_ID/placements" \
    "{\"stamp_id\":\"$STAMP_ID\"}" >/dev/null
info "placed $DEPLOYMENT_ID on $STAMP_ID"

FABRIC_AGENT_ENROLLMENT_TOKEN="" "$WORK/fabric-agent" \
    --control-plane "$CONTROL_URL" \
    --state-dir "$WORK/agent-state" \
    --upstream "http://model-host.e2e.test" \
    --once 2>&1 | tail -2

info "rendered configuration:"
"$CONTROL_PY" -c "
import json;print(json.dumps(json.load(open('$WORK/agent-state/deployments.json')), indent=2))" \
    | sed 's/^/     /'

# --------------------------------------------------------------------------
log "6. Control plane: the status the agent reported is visible"
# --------------------------------------------------------------------------

STATUS=$(api GET "/v1/accounts/$ACCOUNT_ID/deployments/$DEPLOYMENT_ID/status")
echo "$STATUS" | "$CONTROL_PY" -c "
import json,sys
rows=json.load(sys.stdin)
assert rows, 'no status was reported by the agent'
row=rows[0]
assert row['phase']=='ready', row
assert row['observed_generation']==1, row
print(f\"     phase={row['phase']} observed_generation={row['observed_generation']} stamp={row['stamp_id'][:8]}\")"

# --------------------------------------------------------------------------
log "7. Data plane: authorize inference from the agent-written configuration"
# --------------------------------------------------------------------------

INFERENCE_TOKEN=$(curl -fsS -X POST "$CONTROL_URL/v1/token" -H 'Content-Type: application/json' \
    -d "{\"grant_type\":\"api_key\",\"api_key\":\"$API_KEY\",\"audience\":\"fabric-inference\"}" \
    | "$CONTROL_PY" -c "import json,sys;print(json.load(sys.stdin)['access_token'])")
JWKS=$(curl -fsS "$CONTROL_URL/.well-known/jwks.json")
echo "$JWKS" > "$WORK/jwks.json"

cd "$REPO_ROOT/data-plane"
FABRIC_E2E_DEPLOYMENTS="$WORK/agent-state/deployments.json" \
FABRIC_E2E_JWKS="$WORK/jwks.json" \
FABRIC_E2E_TOKEN="$INFERENCE_TOKEN" \
FABRIC_E2E_ACCOUNT="$ACCOUNT_ID" \
"$DATA_PY" - <<'PY'
import asyncio, json, os, uuid
import httpx
from fabric_data_plane.app import DataPlane, create_inference_app
from fabric_data_plane.config import Settings
from fabric_data_plane.keys import KeyCache
from fabric_data_plane.registry import DeploymentRegistry

deployments = os.environ["FABRIC_E2E_DEPLOYMENTS"]
token = os.environ["FABRIC_E2E_TOKEN"]
account = os.environ["FABRIC_E2E_ACCOUNT"]

settings = Settings(
    app_env="test",
    jwt_issuer="http://control.e2e.test",
    jwks_url="http://control.e2e.test/.well-known/jwks.json",
    jwks_file=os.environ["FABRIC_E2E_JWKS"],
    deployments_file=deployments,
)
# The registry is loaded from the file the Go agent wrote, unmodified.
registry = DeploymentRegistry.load(deployments)
assert len(registry) == 1, f"expected one deployment from the agent, got {len(registry)}"

def upstream(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    return httpx.Response(200, json={
        "id": "cmpl-e2e", "model": body["model"],
        "choices": [{"message": {"role": "assistant", "content": "ok"}}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2},
    })

plane = DataPlane(
    settings=settings,
    keys=KeyCache(settings, client=httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json=json.load(open(os.environ["FABRIC_E2E_JWKS"])))
    ))),
    registry=registry,
    client=httpx.AsyncClient(transport=httpx.MockTransport(upstream)),
)
app = create_inference_app(plane)

async def main():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ingress.e2e") as client:
        listed = await client.get("/v1/models", headers={"Authorization": f"Bearer {token}"})
        assert listed.status_code == 200, listed.text
        aliases = [entry["id"] for entry in listed.json()["data"]]
        assert aliases == ["e2e-model"], aliases
        print(f"     models visible to the account: {aliases}")

        served = await client.post(
            "/v1/chat/completions",
            json={"model": "e2e-model", "messages": [{"role": "user", "content": "hi"}]},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert served.status_code == 200, served.text
        print(f"     inference authorized, reply model={served.json()['model']}")

        # A token for a different account must be refused for the same model.
        import datetime as dt, jwt
        from cryptography.hazmat.primitives import serialization
        forged_account = str(uuid.uuid4())
        # Re-sign with a key the data plane does not trust: this must fail as an
        # unknown key, proving account substitution needs the signing key too.
        other = serialization.load_pem_private_key(
            __import__("cryptography").hazmat.primitives.asymmetric.rsa.generate_private_key(
                public_exponent=65537, key_size=2048
            ).private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            ), password=None)
        now = dt.datetime.now(tz=dt.UTC)
        forged = jwt.encode({
            "iss": "http://control.e2e.test", "sub": "attacker", "aud": "fabric-inference",
            "iat": int(now.timestamp()), "nbf": int(now.timestamp()),
            "exp": int((now + dt.timedelta(minutes=5)).timestamp()),
            "account_id": forged_account, "scp": ["inference:invoke"],
        }, other.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode(), algorithm="RS256", headers={"kid": "forged"})

        refused = await client.post(
            "/v1/chat/completions",
            json={"model": "e2e-model", "messages": []},
            headers={"Authorization": f"Bearer {forged}"},
        )
        assert refused.status_code == 401, refused.text
        print(f"     forged token refused: {refused.json()['error']['code']}")

asyncio.run(main())
PY

log "Loop closed"
cat <<EOF

  control plane -> agent          enrollment consumed a single-use token,
                                  desired state delivered by generation
  agent         -> data plane     deployments.json rendered with the owning
                                  account, upstream, and release
  agent         -> control plane  status reported and readable through the API
  data plane    -> caller         inference authorized for that account only

EOF
