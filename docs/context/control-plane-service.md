# Control-Plane Service

**Status:** Implemented (initial version) — the service in [`control-plane/`](../../control-plane/) runs, migrates, and passes its test suite.  
**Design reference:** [Control-Plane Data and API Design](control-plane-data-api-design.md)  
**Not included yet:** inference data plane, cluster agent, operator, telemetry pipeline, and console UI.

## What runs today

| Capability | State |
|---|---|
| Auth0 access-token validation with cached JWKS | Implemented |
| Just-in-time user provisioning from the Auth0 subject | Implemented |
| Account creation with owner membership | Implemented |
| Account roles mapped to server-derived scopes | Implemented |
| Last-owner protection on membership changes | Implemented |
| `POST /v1/token` for Auth0 and API-key exchange | Implemented |
| Fabric RS256 JWT signing and JWKS publication | Implemented |
| API-key create/list/revoke with verifier-only storage | Implemented |
| API-key scopes bounded by the creating principal's scopes | Implemented |
| Service principals owning machine API keys | Implemented |
| Deployment intent with monotonic generations | Implemented |
| Placement authorization for BYOI and managed stamps | Implemented |
| Supported-orchestrator gate for managed placement | Implemented |
| Stamp enrollment with separate agent and telemetry credentials | Implemented |
| Single-use enrollment tokens claimed atomically | Implemented |
| Heartbeat, desired-state pull, and status reporting (BYOI and managed) | Implemented |
| Stamp and enrollment-token revocation | Implemented |
| Audit events and transactional outbox rows | Implemented |
| Row-level security policies | Not implemented; application and composite keys enforce tenancy |
| Usage ingestion | Implemented; authenticated by the telemetry credential, ownership from the placement |
| Managed-capacity entitlement management API | Not implemented; the flag is set directly in the database |
| Request idempotency | Not implemented; `idempotency_keys` exists with no handler |
| Machine-credential rotation | Not implemented; credentials are revocable but not rotatable |

## Layout

```text
control-plane/
├── app/
│   ├── api/v1/        accounts, service_principals, api_keys, deployments,
│   │                  stamps, tokens, system
│   ├── core/          config, database, security, auth0, jwt_service, credentials,
│   │                  scopes, platform, errors, timeutil
│   ├── services/      accounts, service_principals, api_keys, deployments,
│   │                  identity, stamps, audit
│   ├── models.py      all account-scoped tables
│   ├── schemas.py     request/response contracts
│   ├── cli.py         seed-system-account
│   └── main.py        ASGI app
├── migrations/        Alembic history (0001_initial)
├── tests/             accounts, identity, service principals, API keys,
│                      deployments, stamps, config, system/schema/OpenAPI parity
└── openapi.json       exported spec for frontend client generation
```

## Auth0 configuration required

The tenant `https://fabric.jp.auth0.com/` is confirmed to expose JWKS at
`https://fabric.jp.auth0.com/.well-known/jwks.json`.

To finish wiring, create the following in Auth0 and supply the values as environment variables:

1. **API (resource server)** representing the Fabric control API.
   - Set an identifier such as `https://api.fabric.example/control`; this becomes `FABRIC_AUTH0_AUDIENCE`.
   - Signing algorithm: **RS256**.
   - This identifier must be requested as the `audience` by the frontend, otherwise Auth0 issues an opaque token that Fabric cannot verify.
2. **Single-page application** client for the console.
   - Record the client ID for the frontend (the backend does not need it).
   - Configure callback, logout, and web-origin URLs for local and deployed console hosts.
3. Optional **machine-to-machine** client if a non-human integration must obtain Auth0 tokens directly.

The backend only needs the issuer and audience. It never needs an Auth0 client secret, and no Auth0 credential is stored in Kubernetes.

## Environment variables

Copy [`control-plane/.env.example`](../../control-plane/.env.example) to `.env` and set:

| Variable | Purpose |
|---|---|
| `FABRIC_DATABASE_URL` | Async PostgreSQL URL, for example `postgresql+asyncpg://fabric:fabric@localhost:5432/fabric` |
| `FABRIC_AUTH0_ISSUER` | `https://fabric.jp.auth0.com/` |
| `FABRIC_AUTH0_AUDIENCE` | Identifier of the Auth0 API created above |
| `FABRIC_JWT_ISSUER` | Issuer claim for Fabric-signed tokens |
| `FABRIC_JWT_PRIVATE_KEY_PATH` | PEM RSA private key used to sign Fabric JWTs |
| `FABRIC_CREDENTIAL_PEPPER` | Server-side pepper for credential verifiers; required outside `local`/`test` |
| `FABRIC_SYSTEM_ACCOUNT_SLUG` | Slug of the protected internal account |

Outside `local`/`test`, the service refuses to start without a signing key. In `local`/`test` it generates an ephemeral key so tokens do not survive a restart.

## Local commands

```bash
cd control-plane
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e '.[dev]'

# Generate a local signing key (never commit it)
openssl genrsa -out signing.pem 2048

# Apply migrations
.venv/bin/python -m alembic upgrade head

# Create the protected system account that owns managed stamps
.venv/bin/python -m app.cli seed-system-account

# Run the API
.venv/bin/python -m uvicorn app.main:app --reload --port 8080

# Quality gates
.venv/bin/ruff check app tests migrations
.venv/bin/python -m pytest -q
```

Tests run against a temporary SQLite database created by the real Alembic
migration, so the migration history is exercised on every run. PostgreSQL remains
the deployment target; the schema is written to be portable across both.

## Endpoints

```text
GET  /healthz
GET  /readyz
GET  /.well-known/jwks.json
POST /v1/token

GET  /v1/me
POST /v1/accounts
GET  /v1/accounts/{account_id}
GET  /v1/accounts/{account_id}/members
POST /v1/accounts/{account_id}/members

POST   /v1/accounts/{account_id}/service-principals
GET    /v1/accounts/{account_id}/service-principals
DELETE /v1/accounts/{account_id}/service-principals/{principal_id}

POST   /v1/accounts/{account_id}/api-keys
GET    /v1/accounts/{account_id}/api-keys
DELETE /v1/accounts/{account_id}/api-keys/{key_id}

POST   /v1/accounts/{account_id}/deployments
GET    /v1/accounts/{account_id}/deployments
GET    /v1/accounts/{account_id}/deployments/{deployment_id}
PATCH  /v1/accounts/{account_id}/deployments/{deployment_id}
DELETE /v1/accounts/{account_id}/deployments/{deployment_id}
POST   /v1/accounts/{account_id}/deployments/{deployment_id}/placements
GET    /v1/accounts/{account_id}/deployments/{deployment_id}/placements
GET    /v1/accounts/{account_id}/deployments/{deployment_id}/status

POST /v1/accounts/{account_id}/stamp-enrollment-tokens
DELETE /v1/accounts/{account_id}/stamp-enrollment-tokens/{token_id}
GET  /v1/accounts/{account_id}/stamps
DELETE /v1/accounts/{account_id}/stamps/{stamp_id}
POST /v1/stamps/enroll
POST /v1/stamps/{stamp_id}/heartbeat
GET  /v1/stamps/{stamp_id}/desired-state
POST /v1/stamps/{stamp_id}/status
```

`/v1/me` and `POST /v1/accounts` authenticate with an Auth0 access token because a
new user has no account context yet. Every other account route requires a Fabric
control token whose `account_id` matches the path. Stamp routes authenticate with
the stamp's own credential, and the credential — not the path — is authoritative.
Revoking a stamp also revokes its agent and telemetry credentials, after which
heartbeat, desired-state, and status calls are denied.

## Error envelope

```json
{ "error": { "code": "account_selection_required", "message": "...", "details": {} } }
```

Notable codes: `account_membership_required` (403), `account_selection_required`
(400), `invalid_account_selection` (403), `account_mismatch` (403),
`insufficient_scope` (403), `scope_not_delegable` (403), `audience_not_permitted`
(403), `api_key_revoked` (401), `principal_inactive` (401),
`service_principal_not_found` (404), `service_principal_name_taken` (409),
`last_owner_required` (409),
`enrollment_token_used` (401), `enrollment_token_revoked` (401),
`enrollment_token_not_found` (404), `credential_revoked` (401),
`stamp_mismatch` (403), `stamp_revoked` (403), `managed_capacity_not_enabled`
(403), `unsupported_orchestrator` (403), `stamp_not_available` (403),
`placement_not_found` (400), `placement_not_available` (403).

## Frontend integration

`control-plane/openapi.json` is committed for client generation. Regenerate after
API changes:

```bash
.venv/bin/python -c "import json;from app.main import app;print(json.dumps(app.openapi(),indent=2))" > openapi.json
```

Console flow: complete Auth0 login requesting the Fabric API audience, call
`GET /v1/me`, create an account if none exists, exchange the Auth0 token at
`POST /v1/token` (sending `account_id` when the user belongs to several accounts),
then use the returned control token as `Authorization: Bearer <token>`.

## Operational notes

- The API requires TLS termination and a reverse proxy in front of it; it performs no TLS itself.
- Outside `local`/`test` the service refuses to start without a signing key, and also without a real `FABRIC_CREDENTIAL_PEPPER` — the default and the `.env.example` placeholder are both rejected, because every credential verifier derives from it.
- Managed capacity is gated by `accounts.managed_capacity_enabled`, which is currently set directly in the database.
- Managed placement additionally requires a supported Kubernetes distribution (`aks`, `k3s`).
- Usage ingestion is implemented on `POST /v1/telemetry/usage`, authenticated by the
  telemetry credential; ownership comes from that credential and the placement.
  `GET /v1/accounts/{account_id}/deployments/{deployment_id}/usage` reads totals back.
  The `fabric-collector` binary performs the drain-and-forward.
- Outbox delivery workers are defined in the schema but have no publisher yet.
- Pooled PostgreSQL connections are recycled below the usual idle timeout. Managed
  offerings close idle connections, and pre-ping alone leaves a race in which a
  dropped connection fails mid-statement and surfaces as a 500. Observed against a
  managed instance while an agent reported status.
- Stamp revocation is available through the API; agent and telemetry credential rotation is not.
- Verified locally: `ruff` clean, 55 tests passing, `alembic upgrade → downgrade → upgrade` reproducing 16 application tables, 35 routes, and `openapi.json` matching the running app (24 paths, 38 schemas) — enforced by a test.
