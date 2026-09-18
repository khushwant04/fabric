# ADR 0012: A release is drained only after the data plane acknowledges it

**Decision status:** Accepted  
**Implementation status:** Implemented  
**Date:** 2026-09-16

## Context

A model-host Deployment uses `Recreate` because an in-place rolling update deadlocks when
the replacement pod requests the GPU still held by the old pod. Recreate also takes the
model offline for its full cold start, measured at roughly 510 seconds. ADRs 0010 and 0011
made one deployment a health-aware, weighted backend pool, so two separately named workloads
can coexist and traffic can move without changing the active workload.

Changing a weight to zero is not enough to delete a workload safely. Kubernetes may delay a
ConfigMap projection, and an already-running streamed request keeps its backend lease after
new selection has stopped. Deleting before every data-plane process loaded the cutover and
old leases reached zero drops requests.

## Decision

Rollout is a durable state machine recorded in `FabricModelDeployment.status.rollout` and
reconstructable from deterministic workload names, labels, and the rendered route document:

1. **Serving.** One active workload/release is positively routed.
2. **Preparing.** A deterministic release-addressed candidate Deployment and headless Service
   are created beside the active workload. Active routing remains unchanged. Candidate
   readiness requires every requested replica plus concrete ready EndpointSlice addresses;
   the Service DNS fallback is not promotion evidence.
3. **Draining.** Once ready, the operator publishes one weighted pool with candidate total
   weight `1` and active total weight `0`. It records the exact per-deployment route revision
   and old backend ids. Relative release weight is divided by ready endpoint count, so replica
   count does not multiply a release's share.
4. **Promotion.** The data plane exposes only non-secret router state on a dedicated internal
   listener: loaded revision and per-backend in-flight counts. The operator waits until the
   revision matches and every old backend is zero. It then publishes the candidate alone under
   the deployment's declared strategy, deletes the drained workload and Service, and records
   the candidate as active.
5. **Rollback.** A candidate missing its readiness deadline is deleted while the untouched
   active route remains. Once cutover starts, rollback first republishes active weight `1` and
   candidate weight `0`, then uses the same acknowledgement/drain rule before candidate
   deletion. A first release has no rollback target and reports unavailable failure.

The router-status listener is separate from both public inference and destructive administration.
The Helm chart publishes it through a ClusterIP Service selected only by the operator, and a
NetworkPolicy permits only operator pods. The current stamp has one data-plane pod. If that is
scaled later, acknowledgement must query every pod rather than a load-balanced sample before
cleanup.

A rollout never falls back to destructive Recreate while claiming zero downtime. If spare GPUs
are unavailable, the candidate remains Pending, times out, and the active release continues.
Coexistence costs one additional deployment-sized GPU allocation during preparation and drain.

`MaxParallel` counts durable preparing/draining/rollback phases, so an operator restart does not
forget the stamp's rollout budget.

## Consequences

### Positive

- The active model remains continuously routable through candidate cold start and failed rollout.
- No old pod is deleted while a loaded route or active ordinary/streamed request still references it.
- Failed ConfigMap publication cannot strand a document pointing at a deleted workload, because
  destructive cleanup occurs only after publication and acknowledgement.
- Deterministic names and status checkpoints let a restarted operator resume rather than restart a
  rollout or lose its last good release.

### Negative

- Zero downtime requires spare GPU capacity for the coexistence window.
- Rollout now spans multiple reconciles and depends on a private data-plane acknowledgement
  listener.
- Current acknowledgement assumes one data-plane pod. Horizontal data-plane scaling must add
  per-pod acknowledgement before it is safe.
- M3 performs a readiness-gated 0/100 cutover, not an SLO-driven canary. Intermediate percentages
  need an explicit promotion signal and are deferred.

## Alternatives considered

- **Patch the active Recreate workload first, then create an old-release sidecar:** rejected because
  it stops the known-good server before a safe copy exists.
- **Delete after writing weight zero:** rejected because successful ConfigMap update is not proof
  that the data plane loaded it or that streams finished.
- **Use a fixed drain sleep:** rejected because stream duration is not bounded by a fixed grace
  period.
- **Expose the existing admin listener:** rejected because it leases and acknowledges usage and
  exposes internal state; it is intentionally localhost-only.
- **Fall back to Recreate without spare capacity:** rejected because it contradicts the milestone's
  availability guarantee; preserving the old release and reporting insufficient capacity is honest.

## Verification

Tests cover initial serving, candidate creation without active mutation, partial/full readiness,
concrete endpoint gating, cutover revision, zero-weight selection, ordinary and streamed drain,
ack mismatch, restart in every durable phase, timeout rollback, first-release failure,
mid-rollout desired-generation change, MaxParallel after restart, ConfigMap write failure before
cleanup, exact-selector non-collision, weight normalization, and cleanup only after route
acknowledgement and zero in-flight counts.
