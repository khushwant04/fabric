# Security and Identity Design

**Status:** Implemented for the control plane, the inference ingress, and the stamp — the human flow, API-key flow, Fabric JWT contract, credential classes, local inference-token verification, usage-ingestion authorization, and the agent's and collector's credential separation described here run in [`control-plane/`](../../control-plane/), [`data-plane/`](../../data-plane/), and [`agent/`](../../agent/).

**Defence in depth:** PostgreSQL row-level security isolates accounts behind the service checks, so an unfiltered query returns nothing rather than another tenant's rows. It binds only to a database role without `BYPASSRLS`, which the control plane verifies at startup. See [Current State](current-state.md) for the context model and the paths that are deliberately elevated.

**Not implemented:** machine-credential rotation, mTLS between the data plane and a model host, and quotas or rate limiting.

## Trust model

Auth0 authenticates humans. Fabric owns accounts, account memberships, service principals, API keys, scopes, resource authorization, and token audiences. Data planes trust short-lived Fabric tokens and local deployment configuration, not the availability of central services.

## Human flow

1. User completes Auth0 Universal Login.
2. Fabric token service validates issuer, signature, audience, and expiry using Auth0 JWKS.
3. Fabric maps the Auth0 subject to a local user and active account memberships.
4. Zero active memberships returns `account_membership_required` with no token; one may be selected implicitly; multiple require a valid requested account or return `account_selection_required` with no token.
5. Foreign/inactive account selection is rejected and a successful exchange issues exactly one short-lived account-bound `fabric-control` JWT.
6. Control API authorizes actions from local membership and policy.

## API-key flow

1. Authorized user creates a scoped key, either for an account-owned service principal or as an explicitly supported human development key.
2. Fabric generates a high-entropy secret and displays it once.
3. Fabric stores key ID/prefix, a verifier, authoritative `account_id`, principal owner, scopes, restrictions, expiry, and revocation state.
4. SDK sends the key only to the token endpoint; any supplied account selector is ignored.
5. Token service derives account/scopes solely from the stored key record, validates the secret and restrictions, rejects keys whose service principal is inactive, and returns a short-lived audience-bound Fabric JWT.
6. SDK caches the JWT and uses it for the authorized operation.

Keys are account-owned rather than membership-owned: revoking a user's membership does not revoke keys that user created, so key revocation is an explicit step during offboarding.

The credential format is `fab_<class>_<credential-id-hex>_<random-secret>`, where the class is `key`, `enroll`, `agent`, or `telem`. The presented class must match the class being verified, so one credential type can never be used as another. Storage is `HMAC-SHA256(pepper, "<credential-id>:<secret>")` compared in constant time; raw secrets are never persisted, and the pepper must be a real secret outside `local`/`test`.

A key may only carry a control scope that the principal creating it already holds; `inference:invoke` is delegable because no control role holds it.

## Fabric JWT contract

Required claims:

```text
iss, sub, aud, iat, nbf, exp, jti
account_id, scp, principal_type
```

- `aud` is exactly `fabric-control` or `fabric-inference`.
- `scp` is server-derived and least privilege.
- `principal_type` distinguishes human and service principals; `account_id` is derived from Fabric membership or API-key ownership.
- Token lifetime is short enough to bound revocation staleness.
- Signing keys have persistent single-writer rotation and overlap longer than token lifetime plus verifier cache skew.

## Data-plane authorization

Ingress/router validates token signature and claims locally. It derives account identity from verified claims and deployment identity from the matched route. Client ownership headers are stripped or overwritten. Runtime Services are reachable only from the authorized ingress/router identity through network and workload policy.

Implemented in [`data-plane/`](../../data-plane/):

- Verification is local. Signing keys are fetched from the control plane's JWKS
  document and cached; a request performs no control-plane call once a usable key
  set is held, and an unknown `kid` triggers at most one refresh. A fetch failure
  never discards cached keys, so already-issued tokens keep working through a
  control-plane outage (AR-DP02). The cache can also be seeded from a local file
  so a restart mid-outage can still verify.
- A token must carry `aud=fabric-inference`, the configured issuer, valid time
  claims, an `account_id`, and the `inference:invoke` scope. A control-audience
  token is rejected outright.
- Deployments come from a local file the cluster agent writes, never from a
  control-plane lookup. A caller selects a deployment by model name, which carries
  no authority: ownership is decided by comparing the verified token's account
  against the account recorded for that deployment, so naming another account's
  model is refused rather than served.
- `Authorization` and any `X-Fabric-Account-Id`, `X-Fabric-Deployment-Id`,
  `X-Fabric-Stamp-Id`, or `X-Fabric-Principal` header a client sends is dropped
  before the request reaches the model host (AR-ID05).
- The response reports the customer's model alias, not the internal release name
  the model host is addressed by.
- The administrative listener (health, readiness, key-cache and usage state) is a
  separate application on a separate port from the inference listener (AR-DP03).

Not implemented: request quotas and rate limiting, mTLS or network policy between
the ingress and the model host, and central telemetry export. Usage is buffered
locally in a bounded queue with no exporter.

## Control-plane authorization

Every tenant product record is account scoped. Cluster registration, placement, and deployment delivery use workload identities distinct from user credentials. The control API has no GPU runtime credential and the runtime has no general control-plane database credential.

## Cluster enrollment and identity

- Managed and BYOI stamps have control-plane-issued IDs bound to authoritative account ownership and operating mode. Managed stamps belong to the Fabric system account; BYOI stamps belong to the enrolling customer account.
- A short-lived, single-use account-bound enrollment token is exchanged over TLS for a revocable stamp-scoped credential and then discarded. The server ignores caller-supplied account ownership.
- A scoped HTTPS token is acceptable for testing; rotating mTLS credentials are the production direction.
- Agent credentials authorize only heartbeat, capabilities, desired-state reads, and status writes for that stamp. A separate collector credential authorizes write-only telemetry. Neither is a user, control, or inference credential.
- Enrollment, credential issuance/revocation, ownership changes, and privileged stamp operations are audited.

## Agent and operator isolation

- Cluster agents initiate outbound authenticated communication; routine orchestration requires no public Kubernetes API or SSH access.
- Agent RBAC permits Fabric CR and selected status/capability reads, not arbitrary Secret reads or direct mutation of operator-owned generated workloads.
- Operator RBAC is limited to its CRDs and generated inference resources.
- Agent, operator, collector, runtime, and ingress use distinct ServiceAccounts where practical.
- VM bootstrap uses managed identity for registry/model/secret access.
- Benchmark and production identities, networks, and secrets are separate.

## BYOI trust boundary

The customer account owns the BYOI stamp while the customer owns the Kubernetes control plane, nodes, networking, and availability. Fabric owns only installed components and resources permitted by the Helm bundle RBAC. Capability reports use an allowlisted schema and exclude Secrets, arbitrary labels/annotations, node files, prompt/response content, and unnecessary customer metadata.

## Telemetry authentication and privacy

- OTLP/remote-write export uses TLS and a separate write-only telemetry credential in a collector-only Secret/ServiceAccount, including during testing.
- Central ingestion resolves authoritative account/stamp ownership from the credential, verifies deployment placement, and overwrites untrusted account/stamp telemetry attributes.
- Prompt/response content is excluded from logs, metrics, and traces by default.
- Metric labels use bounded control-plane-issued IDs; authorization values and unbounded request IDs are prohibited.
- Usage events carry identifiers/counts needed for operations or future metering, not credentials or raw content.
- Telemetry queues are bounded and never create request-path backpressure.

## Supply chain

- Deploy immutable image digests, never mutable production tags.
- Allowlist registries and verify signatures/provenance.
- Pin model/tokenizer revisions and verify checksums.
- Scan runtime images and dependencies.
- Restrict operator-created pods, capabilities, host mounts, and service accounts.
- Sign/version the BYOI Helm bundle and component images.

## Threats requiring tests

- Forged, expired, wrong-issuer, wrong-audience, or over-scoped JWT.
- API-key enumeration, replay, leakage, and revoked-key exchange.
- Enrollment-token replay or use after expiry.
- Stamp credential used for another tenant/stamp or after revocation.
- Malicious capability/status payload or high-cardinality telemetry.
- Cross-tenant deployment access.
- Identity-header spoofing and direct runtime Service bypass.
- Malicious image/model/chart reference.
- Compromised cluster agent, operator, or collector credential.
- Secret/content leakage in logs, metrics, traces, and benchmark artifacts.
- Denial through unbounded context, output, concurrency, streaming, or telemetry queues.

## Deferred identity work

Agent-session identity and multi-model cache authorization are deferred with agent-aware inference. They must not be implicitly added to the MVP token contract.
