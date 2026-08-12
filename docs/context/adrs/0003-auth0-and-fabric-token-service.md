# ADR 0003: Use Auth0 and a Fabric Token Service

**Decision status:** Accepted  
**Implementation status:** Implemented in the control plane — Auth0 validation, membership resolution, API-key exchange, and RS256 Fabric JWT issuance with JWKS publication run in [`control-plane/`](../../../control-plane/). No data plane verifies the issued inference tokens yet.  
**Date:** 2026-08-11

## Context

Fabric needs human login, service credentials, tenant authorization, and a data plane that authenticates without central calls. Outsourcing all product identity to an external IdP would not model Fabric resources; accepting long-lived API keys at every inference backend would increase exposure and revocation complexity.

## Decision

- Auth0 authenticates human users.
- Fabric owns accounts, account memberships, service principals, roles/scopes, deployment ownership, and API keys.
- A Fabric token endpoint validates an Auth0 assertion or Fabric API key and issues a short-lived JWT.
- Control and inference tokens use distinct audiences: `fabric-control` and `fabric-inference`.
- Data planes verify Fabric JWTs locally using cached/published keys and local resource configuration.
- API-key secrets are high entropy, shown once, and stored only as verifiers plus metadata.
- Account ownership, enrollment tokens, and stamp credentials follow [ADR 0008](0008-account-scoped-tenancy-and-stamp-credentials.md).

The STS pattern is conceptual; Fabric is not adopting an unrelated existing STS codebase or Hexel CIAM.

## Consequences

### Positive

- Human login uses a mature IdP while product authorization remains under Fabric control.
- Long-lived API keys do not traverse the normal inference path.
- Data planes remain independent of Auth0 and databases.

### Negative

- Fabric must operate signing-key rotation, JWKS, token exchange, membership mapping, key lifecycle, and audit.
- Revocation is bounded by short token lifetime unless additional local mechanisms are added.

## Alternatives considered

- **Auth0 directly issues all inference authorization:** rejected because Fabric resource ownership/scopes remain product state.
- **Long-lived API key accepted by runtime:** rejected due to exposure and synchronous validation pressure.
- **Hexel CIAM/STS reuse:** rejected because Fabric is an independent project with different ownership and constraints.

## Verification

Security tests cover wrong issuer/audience, expiry, scope, tenant isolation, key revocation/rotation, header spoofing, backend bypass, and continued inference during central outages.
