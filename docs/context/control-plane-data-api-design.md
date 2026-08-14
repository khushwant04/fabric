# Control-Plane Data and API Design

**Status:** Implemented (initial version) for the control-plane service in [`control-plane/`](../../control-plane/) and the cluster agent in [`agent/`](../../agent/); the operator, telemetry ingestion, RLS policies, and outbox workers described here remain planned.

This document describes the design. Where it once described unbuilt behavior, the
implemented parts are now reconciled against the code, which is the source of
truth. See [Control-Plane Service](control-plane-service.md) for the exact
endpoint list, configuration, and verification commands.

## Scope and ownership rule

Fabric MVP has one customer hierarchy level: **account**. There are no organizations, workspaces, projects, or environment containers.

Every tenant-owned product resource carries an authoritative `account_id`, including memberships, service principals, API keys, deployments, endpoints, BYOI stamps, enrollment tokens, placements, audit events, and usage events. Auth0 users are global authentication identities rather than tenant resources; account access exists only through an account-scoped membership.

```text
Account
  +-- memberships
  +-- service principals
  +-- API keys
  +-- deployments/endpoints
  +-- BYOI inference stamps
  +-- enrollment tokens
  +-- placements
  +-- audit and usage events
```

The server derives account ownership from a verified Fabric principal or stamp credential. Caller-provided headers, request bodies, Kubernetes labels, and telemetry labels are never authoritative ownership sources.

## Managed and BYOI ownership

- **BYOI stamp:** owned by the enrolling customer account.
- **Managed-serverless stamp:** owned by an internal Fabric system account because platform capacity may serve deployments from multiple customer accounts.
- **Deployment and placement:** always owned by the customer account, including placement onto a system-account managed stamp.

A managed stamp is eligible only when its mode is `managed`, its owner is the Fabric system account, and the customer account is entitled to that capacity. A BYOI stamp is eligible only when `stamp.account_id == deployment.account_id`.

## Identity model

### Users and memberships

Auth0 authenticates a person. Fabric stores a global user identity keyed by Auth0 subject and an account-scoped membership:

```text
users(id, auth0_subject, email, display_name, status, created_at, updated_at)
account_memberships(account_id, user_id, role, status, created_at, updated_at)
UNIQUE(account_id, user_id)
```

Initial roles are `owner`, `admin`, `developer`, and `viewer`. Product authorization comes from Fabric membership/role data, not Auth0 profile fields.

### Service principals

Automated callers use account-owned service principals. API keys belong to a service principal or an explicitly supported human development principal.

```text
service_principals(id, account_id, name, status, created_by_user_id, created_at, updated_at)
```

A principal is guarded by the `api-keys` scopes because it exists only to own keys. Disabling one revokes every key it owns in the same transaction, and token exchange independently rejects a key whose principal is not active.

## Core PostgreSQL schema

All IDs are globally unique UUIDs/UUIDv7. Timestamps are UTC. Tenant tables include `account_id`; soft deletion is used where audit or credential history requires it.

### Accounts

```text
accounts(id, slug, name, status, is_system, managed_capacity_enabled, created_at, updated_at)
```

Exactly one protected internal account is designated as the Fabric system account for managed infrastructure. A database constraint or migration-controlled singleton key prevents creation, deletion, or reassignment of a second system account. `managed_capacity_enabled` is the managed-capacity entitlement flag read during placement authorization; it is currently set directly in the database and has no management API.

### API keys

```text
api_keys(
  id, account_id, name, principal_type, principal_id,
  key_prefix, secret_verifier, scopes,
  expires_at, revoked_at, last_used_at,
  created_by_user_id, created_at
)
```

The secret is high entropy, shown once, and never stored. The key ID selects the record; verification uses constant-time comparison against an HMAC verifier protected by a server-side pepper stored outside PostgreSQL. The service refuses to start outside `local`/`test` when that pepper is unset or still a placeholder.

Key scopes are validated server-side twice: the scope must exist, and a control scope must already be held by the principal creating the key, so a key can never escalate beyond the role that created it. `inference:invoke` is delegable by any principal allowed to create keys because it authorizes the data plane only and no control role holds it.

### Deployments

```text
deployments(
  id, account_id, name, model_alias,
  desired_spec, generation, status,
  created_by_principal_id, created_at, updated_at, deleted_at
)
```

Public records use a generic model alias. Private release resolution remains outside public metadata.

### Inference stamps

```text
inference_stamps(
  id, account_id, name, mode, region,
  orchestrator, status, capabilities,
  credential_version, last_heartbeat_at,
  created_at, updated_at, revoked_at
)
```

Ownership modes are only `managed` and `byoi`. Kubernetes distribution/orchestrator is a separate field such as `aks`, `k3s`, or another supported distribution. Unknown ownership modes are rejected fail-closed.

### Enrollment tokens

```text
stamp_enrollment_tokens(
  id, account_id, token_prefix, token_verifier, allowed_mode,
  expires_at, used_at, revoked_at,
  created_by_user_id, created_at
)
```

Tokens are single-use, short-lived, shown once, and stored only as verifiers. Consumption is a conditional `UPDATE ... WHERE used_at IS NULL`, so two concurrent enrollments cannot both claim one token; the losing request is rejected. A token may also be revoked before use.

### Stamp credentials

```text
stamp_credentials(
  id, account_id, stamp_id,
  credential_prefix, credential_verifier, scopes,
  version, expires_at, last_used_at, revoked_at, created_at
)
```

The agent credential never authorizes telemetry export. Production may replace the stored verifier with rotating short-lived machine JWTs or mTLS without changing stamp ownership semantics.

### Telemetry credentials

```text
telemetry_credentials(
  id, account_id, stamp_id,
  credential_prefix, credential_verifier, scopes,
  version, expires_at, last_used_at, revoked_at, created_at
)
```

Every environment, including testing, uses a separate write-only telemetry credential stored in a collector-only Kubernetes Secret and ServiceAccount. It cannot read desired state, write deployment status, invoke control APIs, or invoke inference.

### Placements and status

```text
deployment_placements(
  id, account_id, deployment_id, stamp_id,
  desired_generation, observed_generation,
  status, created_at, updated_at
)

deployment_status(
  account_id, deployment_id, stamp_id,
  observed_generation, phase,
  ready_replicas, unavailable_replicas,
  endpoint, conditions, reported_at
)
```

### Audit, usage, idempotency, and outbox

```text
audit_events(id, account_id, actor_type, actor_id, action, resource_type, resource_id, metadata, created_at)
usage_events(id, account_id, deployment_id, stamp_id, input_tokens, output_tokens, occurred_at, received_at, deduplication_key)
idempotency_keys(id, account_id, principal_id, operation, response_reference, expires_at)
outbox_events(id, account_id, event_type, aggregate_type, aggregate_id, payload, created_at, processed_at, attempts)
```

Usage is operational in MVP and is not billing-grade exactly-once accounting.

## Tenant integrity

Application authorization and database integrity both enforce tenant boundaries:

- Security middleware creates a verified principal context containing `account_id`, principal, roles/scopes, and audience.
- Repositories require `account_id` for every tenant query.
- Parent tenant tables expose `UNIQUE(account_id, id)` where needed.
- Placements use a composite foreign key `(account_id, deployment_id)` to the customer deployment, while `stamp_id` references the stamp independently because an authorized managed placement crosses from a customer account to the system account.
- A constraint trigger or the single transactional placement service enforces exactly one fail-closed branch: same-account BYOI, or system-account managed stamp plus active customer entitlement. Unknown modes are rejected.
- `UNIQUE(account_id, deployment_id, stamp_id)` on placement provides the ownership anchor used by deployment status and usage-event references/validation. Status and usage cannot name an unplaced deployment/stamp pair. Because a managed stamp is owned by the system account while the deployment it runs is owned by a customer, a status write resolves its owning account from the placement row selected by the authenticated `stamp_id`, never from the reporting credential's own account and never from the request body.
- PostgreSQL RLS may add defense in depth. Request transactions set `SET LOCAL app.account_id` from verified context and fail closed when absent; the narrowly scoped placement service role may read system-account managed-stamp metadata but cannot bypass customer deployment ownership. Transaction pooling must reset context per transaction.
- Account IDs in URL paths must match the verified principal context.

## Credential classes

Credentials are not interchangeable:

| Credential | Used by | Purpose |
|---|---|---|
| Auth0 access token | Human browser/client | Prove human identity to the Fabric token endpoint |
| Fabric control JWT | Human or service principal | Call account-scoped control APIs |
| Fabric inference JWT | Application client | Invoke an authorized inference deployment |
| Fabric API key | Service/human automation | Exchange for a short-lived control or inference JWT according to key scopes |
| Stamp enrollment token | New cluster agent | One-time registration only |
| Stamp credential | Enrolled cluster agent | Heartbeat, capabilities, desired-state reads, and status writes only |
| Telemetry credential | Collector | Write-only telemetry export in every environment, including testing |

Auth0 tokens and normal API keys are never stored in Kubernetes for stamp operation. Stamp credentials cannot invoke inference or general customer control APIs.

## Fabric token service

`POST /v1/token` validates either an Auth0 access token or a Fabric API key and issues a short-lived Fabric JWT. Auth0 exchange follows an explicit fail-closed branch: zero active memberships returns `account_membership_required` with no token; exactly one active membership may be selected implicitly; multiple active memberships require a valid requested `account_id`, otherwise the service returns `account_selection_required` with no token. Any supplied account selection must resolve to the active user and an active membership; foreign or inactive selections are rejected. A successful exchange issues exactly one account-bound control JWT. API-key exchange ignores any supplied account selector and derives account/scopes solely from the stored key record.

Required Fabric claims:

```text
iss, sub, aud, iat, nbf, exp, jti
account_id, principal_type, scp
```

Audiences are `fabric-control` and `fabric-inference`. Private signing material lives in a key-management/secret system; PostgreSQL stores only key metadata. JWKS publication supports overlapping active/retiring keys.

## BYOI enrollment protocol

1. An authorized control principal creates an enrollment token under an account.
2. The Helm-installed agent presents the one-time token over TLS with a generated local stamp identity and bounded capabilities.
3. The server loads the token record and derives authoritative `account_id`; it ignores any caller-supplied account ownership field.
4. The server creates the BYOI stamp and scoped credential in one transaction, marks the token used, and audits the action.
5. The agent stores the credential in a Kubernetes Secret and discards the enrollment token.
6. Subsequent heartbeat, desired-state, status, and telemetry requests authenticate as that stamp.

Revocation prevents new synchronization. Existing BYOI workloads remain under customer cluster control; public routing and future token authorization policy determine whether Fabric continues directing traffic to a revoked stamp.

## Desired state and placement authorization

Before assignment, the control plane verifies:

```text
deployment.account_id == requesting_principal.account_id
placement.account_id == deployment.account_id
```

For BYOI, including a k3s-distribution cluster:

```text
stamp.mode == byoi
stamp.account_id == deployment.account_id
```

For managed serverless:

```text
stamp.account_id == FABRIC_SYSTEM_ACCOUNT_ID
stamp.mode == managed
stamp.orchestrator is supported
customer account is entitled to managed capacity
```

The supported-distribution allowlist is server-side (`aks`, `k3s`); an agent-reported distribution outside it cannot receive managed placement.

The agent receives only assignments authorized for its authenticated `stamp_id`. Desired-state and status APIs use monotonic generations and idempotent acknowledgements.

Each assignment carries the owning customer `account_id`, taken from the placement. A managed stamp serves several accounts at once, so an agent cannot infer ownership from its own credential, and the data plane needs it to enforce per-account access. A revoked stamp is denied heartbeat, capability refresh, desired-state reads, and status writes, and revoking a stamp revokes its agent and telemetry credentials in the same transaction.

## Trusted Kubernetes metadata

The agent may apply reserved metadata to Fabric CRs and generated resources:

```text
fabric.ai/account-id
fabric.ai/stamp-id
fabric.ai/deployment-id
fabric.ai/managed-by
```

These identifiers are not secrets, but only agent/operator-generated values are trusted. Customer-supplied conflicts are overwritten or rejected. The operator uses them for selection, status, and observability—not as the sole authorization mechanism. A later admission policy may protect the reserved prefix.

The operator has Kubernetes RBAC only and no central account credential. The agent owns the stamp credential, creates/updates Fabric CRs, reads operator-written status, and sends status centrally.

## Telemetry ownership

Central ingestion validates the stamp/telemetry credential, resolves authoritative `stamp_id` and owner account, verifies that a deployment is assigned to that stamp, rejects mismatched labels, and overwrites account/stamp resource attributes before storage. A compromised stamp cannot submit telemetry as another account merely by changing metric labels.

Managed shared stamps may emit metrics for customer deployments only when the placement authorizes that deployment/account combination. Prompt/response content and credentials are excluded.

## FastAPI control-plane modules

The service is a modular monolith. The implemented layout groups routers by
resource and keeps one service module per domain rather than a package per table:

```text
app/
  api/v1/        accounts, api_keys, deployments, stamps, tokens, system
  core/          config, database, security, auth0, jwt_service, credentials,
                 scopes, platform, errors, timeutil
  services/      accounts, api_keys, deployments, identity, stamps, audit
  models.py      all account-scoped tables
  schemas.py     request/response contracts
migrations/      Alembic history
```

Planned additions: `workers/` for the transactional-outbox publisher.

Use FastAPI, Pydantic, SQLAlchemy 2, PostgreSQL, Alembic, and an async PostgreSQL driver. A transactional outbox worker is sufficient before introducing a message bus.

Go is preferred for the Kubernetes operator and cluster agent. Python remains appropriate for the control plane and inference runtime. Service extraction is deferred until scaling, security isolation, or independent release needs justify it.

## Initial APIs

Implemented today, as exported in [`control-plane/openapi.json`](../../control-plane/openapi.json):

```text
POST /v1/token
GET  /.well-known/jwks.json

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

POST   /v1/accounts/{account_id}/stamp-enrollment-tokens
DELETE /v1/accounts/{account_id}/stamp-enrollment-tokens/{token_id}
GET    /v1/accounts/{account_id}/stamps
DELETE /v1/accounts/{account_id}/stamps/{stamp_id}
POST /v1/stamps/enroll
POST /v1/stamps/{stamp_id}/heartbeat
GET  /v1/stamps/{stamp_id}/desired-state?after_generation=<n>
POST /v1/stamps/{stamp_id}/status
```

`GET /v1/me` replaces a plain account listing because a new user has no account context before the first membership exists. `GET /v1/accounts` (cross-account listing) is not implemented; the memberships in `/v1/me` serve that purpose.

For stamp endpoints, the authenticated credential—not the URL alone—determines authoritative stamp and account identity.

Planned and not implemented: telemetry ingestion, usage publication, outbox delivery workers, request idempotency (the `idempotency_keys` table exists but no handler consumes it), machine-credential rotation, and managed-capacity entitlement management.

## Deferred

- Organization/workspace/project hierarchy.
- Cross-account sharing of BYOI stamps.
- Customer-supplied arbitrary runtime images/arguments in managed service.
- Billing-grade metering.
- Message-bus-based desired-state delivery.
- Production rotating mTLS if scoped testing credentials are sufficient initially.
