# Security and Identity Design

**Status:** Planned — no identity, token, API-key, or authorization implementation exists.

## Trust model

Auth0 authenticates humans. Fabric owns product identities, organizations/workspaces, service principals, API keys, scopes, resource authorization, and token audiences. Data planes trust short-lived Fabric tokens and local deployment configuration, not the availability of central services.

## Human flow

1. User completes Auth0 Universal Login.
2. Fabric token service validates issuer, signature, audience, and expiry using Auth0 JWKS.
3. Fabric maps the Auth0 subject to a local user and membership.
4. Fabric issues a short-lived `fabric-control` JWT.
5. Control API authorizes actions from local membership and policy.

## API-key flow

1. Authorized user creates a scoped key.
2. Fabric generates a high-entropy secret and displays it once.
3. Fabric stores key ID/prefix, a verifier, owner, organization/workspace, scopes, restrictions, expiry, and revocation state.
4. SDK sends the key only to the token endpoint.
5. Token service validates it and returns a short-lived `fabric-inference` JWT.
6. SDK caches the JWT and uses it for inference.

A planned key format is `fab_<key-id>_<random-secret>`. Exact cryptographic storage design must be reviewed before implementation; raw secrets are never persisted.

## Fabric JWT contract

Required claims:

```text
iss, sub, aud, iat, nbf, exp, jti
org, workspace, scp, typ
```

- `aud` is exactly `fabric-control` or `fabric-inference`.
- `scp` is server-derived and least privilege.
- `typ` distinguishes human and service principals.
- Token lifetime is short enough to bound revocation staleness.
- Signing keys have persistent single-writer rotation and overlap longer than token lifetime plus verifier cache skew.

## Data-plane authorization

Ingress/router validates token signature and claims locally. It derives tenant identity from verified claims and derives deployment identity from the matched route. Client ownership headers are stripped or overwritten. Runtime Services are reachable only from the authorized ingress/router identity through network and workload policy.

## Control-plane authorization

Every product record is organization/workspace scoped. Cluster registration, placement, and deployment delivery use workload identities distinct from user credentials. The control API has no GPU runtime credential and the runtime has no general control-plane database credential.

## Cluster and VM identity

- AKS/k3s operators use least-privilege local RBAC.
- Cluster agents use outbound mTLS or a similarly authenticated pull mechanism.
- VM bootstrap uses managed identity for registry/model/secret access.
- Kubernetes APIs and SSH are not made public for routine orchestration.
- Benchmark and production identities, networks, and secrets are separate.

## Supply chain

- Deploy immutable image digests, never mutable production tags.
- Allowlist registries and verify signatures/provenance.
- Pin model/tokenizer revisions and verify checksums.
- Scan runtime images and dependencies.
- Restrict operator-created pods, capabilities, host mounts, and service accounts.

## Data and telemetry

Prompt/response content is excluded from logs and traces by default. Usage events carry identifiers and counts needed for operations/metering, not raw credentials. Audit records cover key lifecycle, login/token failure, deployment changes, cluster registration, and privileged operations.

## Threats requiring tests

- Forged, expired, wrong-issuer, wrong-audience, or over-scoped JWT.
- API-key enumeration, replay, leakage, and revoked-key exchange.
- Cross-tenant deployment access.
- Identity-header spoofing.
- Direct runtime Service bypass.
- Malicious image/model reference.
- Compromised cluster agent/operator credential.
- Secret leakage in logs, metrics, traces, and benchmark artifacts.
- Denial through unbounded context, output, concurrency, or streaming connections.

## Deferred identity work

Agent-session identity and multi-model cache authorization are deferred with agent-aware inference. They must not be implicitly added to the MVP token contract.
