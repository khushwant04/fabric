# Architecture Decision Records

**Status:** Accepted decisions; acceptance does not imply implementation.

ADRs record durable choices. Future changes supersede an ADR with a new record rather than silently rewriting its outcome.

| ADR | Decision | Implementation status |
|---|---|---|
| [0001](0001-separate-control-and-data-planes.md) | Separate control and data planes | Planned |
| [0002](0002-vllm-host-with-fabric-kernels.md) | Use vLLM with Fabric Triton kernels | Kernel prototype only |
| [0003](0003-auth0-and-fabric-token-service.md) | Use Auth0 plus Fabric-owned API keys/JWTs | Planned |
| [0004](0004-local-operator-and-single-intent-crd.md) | Use one local operator and one intent CRD | Planned |
| [0005](0005-t4-production-a10-research.md) | T4 production, A10 research, RTX development | Planned deployment; RTX prototype exists |
| [0006](0006-defer-agent-aware-state.md) | Defer agent-aware session/state caching | Deferred |
| [0007](0007-cluster-agent-and-central-telemetry.md) | Use an outbound cluster agent and asynchronous central telemetry | Planned |
| [0008](0008-account-scoped-tenancy-and-stamp-credentials.md) | Use account-scoped tenancy and stamp credentials | Planned |

## ADR status vocabulary

- **Proposed:** Under discussion.
- **Accepted:** Governs planned work.
- **Superseded:** Replaced by a later ADR.
- **Rejected:** Considered and declined.

Each ADR separately states its implementation status because architectural acceptance and shipped behavior are different facts.
