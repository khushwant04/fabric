package operator

import (
	"context"
	"fmt"
	"time"

	"github.com/khushwant04/fabric/agent/internal/kube"
)

// Rollout decides what to do when a declaration changes the model a host serves.
//
// Replacing a model host is not like rolling a stateless service. The GPU cannot be
// shared: a single device serves one server at a time, so two releases can only run at
// once when the stamp has a second GPU for the new one. Loading weights then takes
// minutes before the new server can answer. That window is unavoidable, but a release
// that never becomes ready must not leave the stamp serving nothing indefinitely: the
// previous release worked, and returning to it is better than waiting for a human.
//
// Two shapes of rollout, chosen by capacity rather than preference (ADR 0011):
//
//   - Coexisting (weighted). When the stamp has spare GPU capacity for the new release
//     the operator brings it up beside the old as a separate workload and the data plane
//     serves both, shifting routing weight from the old to the new as the new one's pods
//     become ready. When the new release is fully ready the old workload is drained and
//     removed and the config returns to a single release. The model is never fully
//     offline, because the old release keeps answering until the new one can. This is M2's
//     weighted routing (FEAT-004) applied to a release change.
//
//   - Recreate (in place). When the stamp cannot fit both the honest answer is that the
//     old server must stop before the new one starts: the operator falls back to the
//     original Recreate behaviour rather than deadlocking on a GPU that will never free.
//     There is a cold-start gap in this case, which no amount of routing can hide on a
//     single device, and pretending otherwise would be a lie about the hardware.
//
// Progressive still means one deployment at a time across a stamp, so a bad release is
// discovered on one before it reaches the rest.
type Rollout struct {
	// Deadline for a new release to become ready before it is considered failed.
	// Generous by necessity: weight loading dominates, and a tight deadline would roll
	// back a release that was about to succeed.
	ReadyTimeout time.Duration
	// MaxParallel bounds how many hosts may be mid-change at once on this stamp. One is
	// the useful default: a bad release then costs one deployment rather than all of
	// them.
	MaxParallel int
	// AutoRollback returns to the last release that was observed ready when a new one
	// misses its deadline.
	AutoRollback bool
}

// DefaultRollout is conservative: rolling back is recoverable, while serving nothing is
// not, so the deadline is long and rollback is on.
func DefaultRollout() Rollout {
	return Rollout{ReadyTimeout: 15 * time.Minute, MaxParallel: 1, AutoRollback: true}
}

const (
	// Annotations record what the operator knows about a host's release history. They
	// live on the workload rather than in operator memory so a restart does not forget
	// which release was good.
	annotationRelease     = "fabric.khushwant.dev/release"
	annotationLastGood    = "fabric.khushwant.dev/last-good-release"
	annotationRolloutTime = "fabric.khushwant.dev/rollout-started-at"
	annotationRolledBack  = "fabric.khushwant.dev/rolled-back-from"
	// annotationRollingFrom records the previous release during a coexisting rollout, so
	// a later pass (in this process or another) knows which release the sidecar carries
	// and keeps serving it until the new release is ready. Cleared once the rollout
	// completes and the sidecar is removed.
	annotationRollingFrom = "fabric.khushwant.dev/rolling-from"

	// ConditionProgressing reports that a change is underway, so a reader can tell a
	// host that is starting from one that is broken.
	ConditionProgressing = "Progressing"
)

// releaseOf returns the release a declaration asks for, which is the upstream model name
// when one is given and the alias otherwise.
func releaseOf(item ModelDeployment) string {
	if item.Spec.UpstreamModel != "" {
		return item.Spec.UpstreamModel
	}
	return item.Spec.ModelAlias
}

// rolloutState is what the operator can observe about one host's progress.
//
// The *primary* workload (stable name) is the one that converges to the declared release.
// During a coexisting rollout the *previous* release keeps serving from a separate
// sidecar workload beside it, so the model stays answerable while the primary reloads
// weights (ADR 0011). The state carries what the operator sees of both, plus whether the
// stamp has GPU room to run them at once.
type rolloutState struct {
	// Current is the release configured on the primary workload.
	Current string
	// LastGood is the most recent release observed ready, or empty if none ever was.
	LastGood string
	// Ready reports the primary workload's own readiness on its current release.
	Ready bool
	// ReadyReplicas is how many of the primary's pods are ready, so weight can rise with
	// readiness rather than jump from nothing to everything.
	ReadyReplicas int
	// StartedAt is when the primary's current release began rolling out.
	StartedAt time.Time
	// Exists is false before the primary workload has been created.
	Exists bool
	// DesiredReplicas is the declared replica count, the denominator for the readiness
	// fraction.
	DesiredReplicas int

	// PrevRelease is the release the primary is rolling *from*, preserved so its sidecar
	// can keep serving during the shift. Empty when no rollout is in progress.
	PrevRelease string
	// SidecarExists and SidecarReady describe the previous release's sidecar workload,
	// which carries traffic while the primary's new release is not yet ready.
	SidecarExists bool
	SidecarReady  bool

	// CanCoexist reports whether the stamp has GPU capacity to run the new release beside
	// the old. When false the rollout falls back to Recreate rather than wait for a
	// device that will never free (ADR 0011).
	CanCoexist bool
}

func parseTime(value string) time.Time {
	if value == "" {
		return time.Time{}
	}
	parsed, err := time.Parse(time.RFC3339, value)
	if err != nil {
		return time.Time{}
	}
	return parsed
}

// observeRollout reads the host's release history from the primary workload and, when a
// coexisting rollout is in progress, the previous release's sidecar workload too.
//
// canCoexist reports whether the stamp has GPU room to run a second release beside the
// primary; the operator computes it from the hardware profile and passes it in so
// decideRollout stays pure.
func (r *Reconciler) observeRollout(
	ctx context.Context, item ModelDeployment, canCoexist bool,
) (rolloutState, error) {
	var existing deployment
	err := r.client.Get(ctx, r.deploymentPath(hostName(item)), &existing)
	if kube.IsNotFound(err) {
		return rolloutState{CanCoexist: canCoexist, DesiredReplicas: item.Spec.DesiredReplicas()}, nil
	}
	if err != nil {
		return rolloutState{}, fmt.Errorf("read model host: %w", err)
	}

	annotations := existing.Metadata.Annotations
	readyReplicas := 0
	if existing.Status != nil {
		readyReplicas = existing.Status.ReadyReplicas
	}

	state := rolloutState{
		Current:         annotations[annotationRelease],
		LastGood:        annotations[annotationLastGood],
		Ready:           readyReplicas > 0,
		ReadyReplicas:   readyReplicas,
		StartedAt:       parseTime(annotations[annotationRolloutTime]),
		Exists:          true,
		DesiredReplicas: item.Spec.DesiredReplicas(),
		PrevRelease:     annotations[annotationRollingFrom],
		CanCoexist:      canCoexist,
	}

	// Observe the previous release's sidecar, if one is recorded, so the shift can be
	// driven by what is actually running rather than by assumption.
	if state.PrevRelease != "" && state.PrevRelease != state.Current {
		var sidecar deployment
		sidecarErr := r.client.Get(
			ctx, r.deploymentPath(hostNameForRelease(item, state.PrevRelease)), &sidecar,
		)
		switch {
		case kube.IsNotFound(sidecarErr):
			state.SidecarExists = false
		case sidecarErr != nil:
			return rolloutState{}, fmt.Errorf("read model host sidecar: %w", sidecarErr)
		default:
			state.SidecarExists = true
			state.SidecarReady = sidecar.Status != nil && sidecar.Status.ReadyReplicas > 0
		}
	}

	return state, nil
}

// ReleaseWeight is one release in a weighted rollout and the share of traffic it should
// take. The data plane balances across the union of the releases' backends using the
// weighted strategy (FEAT-004), so a release with twice the weight receives roughly twice
// the traffic. Weights are relative, not required to sum to any particular value.
type ReleaseWeight struct {
	// Release served by this workload.
	Release string
	// Weight is this release's share of traffic relative to the others in the set.
	Weight float64
}

// RolloutDecision is what the operator intends to do for one deployment this pass.
//
// The single-release fields (Release/RolledBack/Deferred) describe the common case: a
// host serving, starting, deferred, or rolled back on one workload. A weighted rollout
// additionally fills Weighted with both releases and their shares; when Weighted has more
// than one entry the data plane splits traffic across them, and Coexist records that a
// second workload is running beside the first.
type RolloutDecision struct {
	// Release to configure on the primary (old) workload. Differs from the declared one
	// only when rolling back. During a weighted rollout it stays the old release until
	// the new one is fully ready, so the primary workload is never disturbed mid-shift.
	Release string
	// RolledBack is set when the declared release was abandoned for the last good one.
	RolledBack bool
	// Deferred is set when the change is postponed because too many hosts are already
	// mid-change on this stamp.
	Deferred bool
	// Reason explains the decision, and becomes the condition reason.
	Reason string

	// Weighted is the set of (release, weight) the data plane should split traffic
	// across. A single entry means one release; two entries mean a coexisting rollout in
	// progress. Weights track the primary's readiness on the new release: the previous
	// release starts with all of the traffic and the new release takes it over as its
	// pods come ready.
	Weighted []ReleaseWeight
	// Coexist is true when the previous release should keep serving from a sidecar
	// workload while the primary rolls to the new release (the stamp had capacity for
	// both). When false a release change falls back to Recreate on the single workload
	// and the model is briefly offline, which a single GPU cannot avoid.
	Coexist bool
	// SidecarRelease is the previous release to keep running in a sidecar workload during
	// a coexisting rollout. Empty unless Coexist is set.
	SidecarRelease string
	// RemoveSidecar is set once the primary is fully ready on the new release, telling the
	// operator to drain and remove the previous release's sidecar workload so the config
	// returns to a single release.
	RemoveSidecar bool
}

// singleRelease is the decision for a host served by exactly one release.
func singleRelease(release, reason string) RolloutDecision {
	return RolloutDecision{
		Release:  release,
		Reason:   reason,
		Weighted: []ReleaseWeight{{Release: release, Weight: 1.0}},
	}
}

// readyFraction is the share of the declared release's desired replicas the primary has
// ready, clamped to [0,1]. It is the weight the new release should take: at zero ready
// pods the previous release (sidecar) carries everything, at all pods ready the new
// release does.
func readyFraction(ready, desired int) float64 {
	if desired < 1 {
		desired = 1
	}
	if ready < 0 {
		ready = 0
	}
	if ready >= desired {
		return 1.0
	}
	return float64(ready) / float64(desired)
}

// decideRollout chooses this pass's release(s) for one deployment.
//
// Pure, so the policy can be tested without a cluster: the interesting cases are a new
// release that stalls, one that has never been ready, a stamp already busy with another
// change, a weighted shift as the new release becomes ready, and a stamp that cannot fit
// both releases and must fall back to Recreate.
func decideRollout(
	policy Rollout,
	item ModelDeployment,
	state rolloutState,
	inFlight int,
	now time.Time,
) RolloutDecision {
	declared := releaseOf(item)

	if !state.Exists {
		return singleRelease(declared, "InitialRollout")
	}

	// The primary is already on the declared release. This is the steady state, the tail
	// of a completed weighted rollout, and the Recreate path all at once.
	if state.Current == declared {
		desired := state.DesiredReplicas
		if desired < 1 {
			desired = 1
		}
		// Fully ready means every desired replica can answer, not merely one: a partial
		// readiness is the middle of a weighted shift, where traffic splits by fraction
		// rather than jumping wholesale to the new release. A caller that reports
		// readiness without a replica count (the steady state, where the count is not
		// interesting) is taken at its word.
		fullyReady := state.Ready && (state.ReadyReplicas == 0 || state.ReadyReplicas >= desired)
		rollingWithSidecar := state.CanCoexist && state.PrevRelease != "" &&
			state.PrevRelease != declared && state.SidecarExists

		if fullyReady {
			// If a previous release's sidecar is still up, the rollout has completed and
			// the sidecar can be drained; weight is wholly on the new release regardless.
			if rollingWithSidecar {
				return RolloutDecision{
					Release:        declared,
					Reason:         "RolloutComplete",
					Coexist:        true,
					SidecarRelease: state.PrevRelease,
					RemoveSidecar:  true,
					Weighted:       []ReleaseWeight{{Release: declared, Weight: 1.0}},
				}
			}
			return singleRelease(declared, "Serving")
		}

		// The primary is rolling to the declared release but is not fully ready yet. When
		// a sidecar of the previous release is carrying traffic, shift weight by readiness
		// rather than treating this as a stall.
		if rollingWithSidecar {
			elapsed := now.Sub(state.StartedAt)
			if policy.AutoRollback && !state.StartedAt.IsZero() && elapsed > policy.ReadyTimeout {
				// The new release missed its deadline. Return all traffic to the previous
				// release, which has been serving from its sidecar throughout, so no
				// request is dropped, and abandon the new release on the primary.
				fallback := state.LastGood
				if fallback == "" || fallback == declared {
					fallback = state.PrevRelease
				}
				return RolloutDecision{
					Release:        fallback,
					RolledBack:     true,
					Reason:         "RolledBackAfterReadyTimeout",
					Coexist:        true,
					SidecarRelease: state.PrevRelease,
					// The primary returns to the fallback release; the sidecar that was
					// serving it is folded back into the primary, so no separate workload
					// is left holding a GPU.
					RemoveSidecar: fallback != state.PrevRelease,
					Weighted:      []ReleaseWeight{{Release: fallback, Weight: 1.0}},
				}
			}

			fraction := readyFraction(state.ReadyReplicas, state.DesiredReplicas)
			weighted := []ReleaseWeight{{Release: state.PrevRelease, Weight: 1.0 - fraction}}
			if fraction > 0 {
				weighted = append(weighted, ReleaseWeight{Release: declared, Weight: fraction})
			}
			return RolloutDecision{
				Release:        declared,
				Reason:         "RollingOutWeighted",
				Coexist:        true,
				SidecarRelease: state.PrevRelease,
				Weighted:       weighted,
			}
		}

		// No sidecar (Recreate path, or a first rollout with no previous release): the
		// model is offline while the single workload reloads. Roll back if it stalls and
		// there is a known-good release to return to.
		elapsed := now.Sub(state.StartedAt)
		if policy.AutoRollback && state.LastGood != "" && state.LastGood != declared &&
			!state.StartedAt.IsZero() && elapsed > policy.ReadyTimeout {
			return RolloutDecision{
				Release:    state.LastGood,
				RolledBack: true,
				// Named for what happened rather than "Failed": the release may be
				// fine and the cluster short of capacity, and the operator cannot tell
				// those apart from readiness alone.
				Reason:   "RolledBackAfterReadyTimeout",
				Weighted: []ReleaseWeight{{Release: state.LastGood, Weight: 1.0}},
			}
		}
		return singleRelease(declared, "Progressing")
	}

	// A different release is declared: a change is beginning. Changing it consumes a slot
	// in the stamp's budget for simultaneous changes, so a bad release costs one
	// deployment rather than all.
	if policy.MaxParallel > 0 && inFlight >= policy.MaxParallel {
		return RolloutDecision{
			Release:  state.Current,
			Deferred: true,
			Reason:   "DeferredWhileAnotherRolloutIsInFlight",
			Weighted: []ReleaseWeight{{Release: state.Current, Weight: 1.0}},
		}
	}

	// Capacity decides the shape (ADR 0011). Without room for a second GPU the two
	// releases cannot coexist, so the operator falls back to Recreate: replace the
	// release on the single workload in place, accepting the cold-start gap that a single
	// device cannot avoid, rather than waiting forever for a GPU that will not free.
	if !state.CanCoexist {
		return RolloutDecision{
			Release:  declared,
			Reason:   "RollingOutRecreate",
			Weighted: []ReleaseWeight{{Release: declared, Weight: 1.0}},
		}
	}

	// Beginning a coexisting rollout. The primary moves to the declared release while the
	// previous release keeps serving from a sidecar; all traffic stays on the previous
	// release until the new one has a ready pod. The operator records PrevRelease so the
	// next pass knows which release the sidecar carries.
	return RolloutDecision{
		Release:        declared,
		Reason:         "RollingOutWeightedStarting",
		Coexist:        true,
		SidecarRelease: state.Current,
		Weighted:       []ReleaseWeight{{Release: state.Current, Weight: 1.0}},
	}
}

// rolloutAnnotations records the decision on the primary workload so the next pass, in
// this process or another, can see the same history.
func (r *Reconciler) rolloutAnnotations(
	decision RolloutDecision, state rolloutState, now time.Time,
) map[string]string {
	annotations := map[string]string{annotationRelease: decision.Release}

	switch {
	case decision.RolledBack:
		annotations[annotationRolledBack] = state.Current
		annotations[annotationRolloutTime] = now.UTC().Format(time.RFC3339)
	case state.Current != decision.Release:
		annotations[annotationRolloutTime] = now.UTC().Format(time.RFC3339)
	case state.StartedAt.IsZero():
		annotations[annotationRolloutTime] = now.UTC().Format(time.RFC3339)
	default:
		annotations[annotationRolloutTime] = state.StartedAt.UTC().Format(time.RFC3339)
	}

	// Record which release the sidecar carries while a coexisting rollout is in flight,
	// so the next pass keeps serving it. Cleared once the sidecar is removed (rollout
	// complete or rolled back), so a settled deployment carries no stale rollout marker.
	if decision.Coexist && !decision.RemoveSidecar &&
		decision.SidecarRelease != "" && decision.SidecarRelease != decision.Release {
		annotations[annotationRollingFrom] = decision.SidecarRelease
	}

	// Only a release observed ready becomes the fallback. Recording the declared one
	// optimistically would let a broken release become the thing rolled back to.
	lastGood := state.LastGood
	if state.Ready && state.Current != "" {
		lastGood = state.Current
	}
	if lastGood != "" {
		annotations[annotationLastGood] = lastGood
	}
	return annotations
}
