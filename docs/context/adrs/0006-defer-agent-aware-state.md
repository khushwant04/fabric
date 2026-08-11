# ADR 0006: Defer Agent-Aware Session State

**Decision status:** Accepted  
**Implementation status:** Deferred  
**Date:** 2026-08-11

## Context

Agent loops could benefit from session-aware cache reuse, but cache ownership, invalidation, routing affinity, security, eviction, and model compatibility substantially expand the system. KV/recurrent state generally cannot be shared across different models because representations and architectures differ.

## Decision

Do not include agent-aware inference, session-aware KV/recurrent-state reuse, cross-model cache sharing, canonical agent memory, or disaggregated prefill in the MVP.

If revisited, use three distinct concepts:

1. A model-independent canonical event log.
2. Structured model-independent memory derived from events.
3. Separate model-specific cache handles/cursors with explicit model/runtime identity.

Never represent a cache from one model as reusable state for another model without a model-specific conversion proven correct.

## Consequences

### Positive

- MVP focuses on runtime correctness, measurable performance, identity, deployment, and operations.
- Avoids premature session protocol and distributed-cache infrastructure.
- Prevents false assumptions about cross-model KV compatibility.

### Negative

- Repeated agent turns cannot exploit Fabric-managed session cache reuse initially.
- Future introduction may require API, routing, storage, and authorization changes.

## Alternatives considered

- **Build session cache into MVP:** rejected because it delays proving the core runtime.
- **Share KV/state across models:** rejected as generally invalid.
- **Store only raw prompts as the future abstraction:** insufficient for structured memory and model-specific cache lifecycle.

## Revisit criteria

Reconsider only after the core T4 runtime passes correctness, performance, isolation, rollout, and cost gates, and after real agent workloads quantify the value of prefix/session reuse.
