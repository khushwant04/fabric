# Risks and Open Questions

**Status:** Living register  
Items remain open until resolved by code, experiment, review, or a new ADR.

## Runtime and numerical risk

| Risk | Impact | Planned mitigation |
|---|---|---|
| FP32 Q/K normalization differs from authoritative model-dtype order | Long-sequence output divergence | Tolerance, drift, logit/token, and task-level tests on all target dtypes |
| Microkernel gain disappears in optimized full model | No product differentiation | Compare against pinned FLA/vLLM; promote only end-to-end wins |
| SM89 tuning regresses SM86/SM75 | T4/A10 slowdown or resource spill | Separate target profiles and compile-resource/profiler gates |
| In-place recurrent state is concurrently reused | Corruption | Exclusive state ownership in scheduler integration and stress tests |
| Packed projection increases memory or hurts GEMM efficiency | Negative end-to-end result | Use measured prototype and established GEMM paths; keep unpacked fallback |
| Long contexts exceed practical T4 memory/latency | OOM or poor service | Establish launch limits from T4 load tests rather than configured model maximum |

## Platform risk

| Risk | Impact | Planned mitigation |
|---|---|---|
| Control-plane coupling leaks into inference | Regional outage becomes global outage | Direct data path, local JWT verification/config, failure tests |
| Token-key rotation or cache policy is wrong | Authentication outage or stale access | Single-writer persistent rotation, overlap, bounded TTL, rollback drills |
| Local resource map is stale | Wrong authorization/routing | Versioned config, fail-closed updates, convergence/readiness conditions |
| Single-node k3s is presented as HA | Incorrect reliability expectations | Explicit one-failure-domain status and separate-stamp failover |
| Operator scope grows into workflow engine | Slow delivery and fragile controller | Keep enrollment/connectivity/status in the agent, telemetry in the collector, and one intent CRD in the operator |
| Agent applies stale or wrong-tenant desired state | Unauthorized or incorrect deployment | Stamp-bound credentials, monotonic generations, ownership checks, idempotent acknowledgement |
| Capability discovery leaks customer metadata | BYOI privacy exposure | Allowlisted bounded schema; exclude Secrets, arbitrary labels, files, and content |
| Telemetry outage/backpressure reaches inference | Latency or availability loss | Separate async collector path, bounded queues, explicit drop policy, outage tests |
| Central metrics labels cause tenant leakage/cardinality explosion | Security and operational failure | Ingestion ownership checks and bounded control-plane-issued labels |
| Dashboard status and metrics disagree | Misleading customer view | Separate schemas, timestamps/freshness indicators, desired/observed generation, reconciliation rules |
| Single cluster credential compromises all components | Large blast radius | Separate/scoped agent and telemetry credentials; distinct ServiceAccounts; revocation tests |
| A10 research environment drifts from production | Misleading claims | Immutable image/model plus exact environment artifacts; T4 remains release gate |

## Security risk

| Risk | Impact | Planned mitigation |
|---|---|---|
| API key appears in logs or storage | Credential compromise | Display once, verifier-only storage, redaction tests, scoped/expiring keys |
| Caller spoofs tenant headers | Cross-tenant access | Identity only from verified claims and matched route; overwrite headers |
| Backend bypasses ingress authorization | Unauthorized inference | Private Service, NetworkPolicy, workload identity, bypass tests |
| Model/image supply-chain compromise | Code/data compromise | Digest pinning, signatures, allowlists, checksums, scanning |
| Usage/trace data captures prompts | Privacy exposure | Content off by default, schema allowlist, retention controls |

## Open product questions

1. Which account roles and permission scopes are sufficient for MVP?
2. Should all automation keys belong to service principals, or are explicitly labeled human development keys also allowed?
3. Which OpenAI-compatible endpoints and fields are mandatory for first clients?
4. What launch context/output/concurrency limits preserve useful T4 economics?
5. Is explicit cluster selection sufficient for MVP, or is basic policy placement required?
6. What availability and latency objectives are commercially appropriate after measurement?
7. What usage retention is needed before billing exists?

## Open technical questions

1. Which vLLM and FLA revisions provide the stable integration baseline?
2. Does the recurrence optimization improve full-model decode on T4 after scheduler overhead?
3. Is causal-convolution fusion or projection packing the next largest target-specific gain?
4. Which kernel configurations avoid spills and maximize bandwidth on SM75 and SM86?
5. What model quality test best detects accumulated recurrent-state divergence?
6. Which control-to-cluster delivery mechanism best satisfies outbound-only connectivity?
7. Should route reconciliation live in the operator or a separate platform controller?
8. What bounded telemetry queue and loss policy is acceptable during isolation?
9. Which AKS T4 SKU/driver/runtime combination will be the production reference?
10. Does A10 tensor parallelism provide any research value beyond a negative comparison?

## Decision triggers

- Introduce NCCL only if tensor-parallel research is explicitly approved.
- Introduce CUDA/PTX only if target profiling demonstrates a Triton/library blocker.
- Expand the CRD only when a working reconcile path requires a field.
- Revisit agent-aware state only after core runtime release gates pass.
- Promote A10 to production only through a separate capacity/security/SLO decision.
