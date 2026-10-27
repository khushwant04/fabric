# ADR 0017: Data-zone residency is enforced at placement and again at the gateway

**Decision status:** Accepted  
**Implementation status:** Implemented in the control plane, agent, operator, data plane, and console  
**Date:** 2026-10-27

## Context

A deployment could already be pinned to a region at placement time (`PlacementCreateRequest.region`),
but only as a one-off filter on that call. Nothing recorded *why* the deployment had to be there,
so nothing else enforced it:

- a later placement without `region` could put the same deployment anywhere;
- a stamp reports its region on every heartbeat, so a stamp admitted in one region could later
  report another and keep serving;
- a client holding a valid inference token could send any prompt to any stamp that served its
  account, regardless of where that stamp was.

Data-protection rules such as GDPR and national data laws are about *where data is processed*. A
residency promise that holds only at the moment of placement is not one a customer can rely on.

## Decision

### 1. Residency is part of the deployment's desired spec

`DeploymentSpec.residency` declares one of three levels:

| Level | Meaning |
|---|---|
| `global` (default) | Any stamp. Every deployment created before this ADR is global, so nothing changes for them. |
| `data_zone` | Only stamps whose region the catalogue puts in `data_zone` (`eu`, `us`, `uk`, `ch`, `ca`, `latam`, `jp`, `kr`, `in`, `au`, `apac`, `me`, `af`). |
| `regional` | Only stamps in an explicit list of up to 16 regions. This also covers regions the catalogue does not know, such as on-premises clusters. |

The control plane validates the combination and refuses inconsistent input (a zone with no name,
a regional list with no regions, fields that do not belong to the level).

### 2. The catalogue lives in one place: the control plane

`control-plane/app/core/data_zones.py` maps provider regions (Azure, AWS and GCP spellings) to
zones. Zones are **disjoint**: a region belongs to at most one zone. If zones overlapped (`jp`
inside `apac`), whether a stamp is "in the zone" would depend on which zone was asked about.

A stamp reports a *region*, never a zone. The control plane looks up the zone and sends both back
in desired state as `data_boundary`. The data plane has no copy of the catalogue, so the gateway and
placement always use the same mapping.

**Trust boundary.** The stamp's region is self-reported. For a customer-owned (BYOI) stamp, the
customer controls the region and is also the party the residency protects. For managed capacity,
Fabric operates the stamp and is responsible for its region being truthful. This ADR adds no
attestation, such as a cloud instance-metadata signature. That is listed as future work.

### 3. Enforcement happens three times, and each check fails closed

1. **Placement** (`placement.check_fit`). A stamp outside the residency is refused with
   `data_residency_not_satisfied` and the reason. Automatic selection skips such stamps.
   Residency is checked before capacity, because no amount of free GPUs makes an out-of-zone
   stamp acceptable.
2. **Editing.** Changing residency is refused (`residency_excludes_existing_placement`) if the new
   policy would exclude a stamp already serving the deployment. Edits that leave residency alone
   are not re-checked, so the owner can still fix everything else.
3. **The gateway, on every request.** The data plane compares the deployment's residency with the
   stamp's `data_boundary` and refuses with `data_residency_not_satisfied` before contacting the
   model host. This catches a stamp that moved after placement. `model=auto` never selects a
   deployment the stamp may not serve, and `/v1/models` does not list one.

An unknown region, a missing boundary (from an agent or control plane that predates this ADR), or a
residency level the gateway does not recognise satisfies only `global`. An unreadable stored policy
is read as the strictest one, never as `global`.

### 4. Callers can bind themselves to a zone too

Residency protects a deployment. Callers also need protecting: an EU client should not be able to
send prompts to a US stamp by mistake.

- An **API key** can be created with `data_zone`. Every inference token it mints carries a
  `data_zone` claim. A request to mint a token for another zone is refused
  (`data_zone_not_permitted`), not quietly overridden.
- Any **token exchange** (API key, Auth0, OIDC) can request `data_zone` to narrow a single token.
  Control-audience tokens cannot be zone-bound, because the control plane holds configuration,
  not prompts.
- A request can carry `routing.data_zone` (via OpenAI `extra_body`) to bind just that request.
  The gateway consumes this field and does not forward it to the model host.

The gateway refuses a zone-bound token or request on a stamp outside that zone with
`data_zone_mismatch`. The token check runs immediately after authentication: before the JSON body
is parsed, and before an audio upload is spooled.

### 5. Clients discover in-zone routes instead of the control plane proxying

Inference never passes through the control plane (ADR 0001), so the control plane cannot route
requests. Instead, `GET /v1/accounts/{id}/inference-routes?model=…&data_zone=…` returns the stamp
endpoints serving a model, filtered by zone or region and ordered healthy first, then ready, then
by stamp id. A client reads this at start-up or after a refusal, never per request. Routes whose
stamp no longer satisfies the residency are left out unless `include_ineligible=true`, which is
for diagnostics. `GET /v1/data-zones` publishes the catalogue without authentication.

## Consequences

- Residency is visible in desired state, the CRD (`spec.residency`, `spec.dataBoundary`), the
  rendered data-plane document, and the console.
- A moved stamp changes the data-plane configuration revision, so the gateway reloads and starts
  refusing within one agent pass. It does not wait for a placement change.
- `fabric_dp_data_zone_refusals_total{binding,reason}` counts gateway refusals. Labels are bounded
  and seeded at zero, so the first refusal is visible to `rate()`.
- Migration `0005_api_key_data_zone` adds a nullable `api_keys.data_zone`. Existing keys remain
  unbound.

## Not decided here

- Attested stamp location (signed cloud metadata) in place of self-reported regions.
- Account-wide residency defaults that every deployment and key inherits.
- Residency for usage and telemetry records. These hold no prompt content but are still processed
  centrally.
- Client-side SDK helpers that automatically fail over between in-zone routes.
