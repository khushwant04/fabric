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
// shared, so the old server must stop before the new one can start, and the new one then
// spends minutes loading weights before it can answer. That window is unavoidable, but a
// release that never becomes ready must not leave the stamp serving nothing
// indefinitely: the previous release worked, and returning to it is better than waiting
// for a human.
//
// This is deliberately narrow. There is no traffic splitting, because one GPU serves one
// server at a time and a percentage of traffic cannot be expressed. Progressive means
// one deployment at a time across a stamp, so a bad release is discovered on one before
// it is applied to the rest.
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
type rolloutState struct {
	// Release currently configured on the workload.
	Current string
	// LastGood is the most recent release observed ready, or empty if none ever was.
	LastGood string
	// Ready reports the workload's own readiness.
	Ready bool
	// StartedAt is when the current release began rolling out.
	StartedAt time.Time
	// Exists is false before the workload has been created.
	Exists bool
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

// observeRollout reads the host's release history from the workload itself.
func (r *Reconciler) observeRollout(
	ctx context.Context, item ModelDeployment,
) (rolloutState, error) {
	var existing deployment
	err := r.client.Get(ctx, r.deploymentPath(hostName(item)), &existing)
	if kube.IsNotFound(err) {
		return rolloutState{}, nil
	}
	if err != nil {
		return rolloutState{}, fmt.Errorf("read model host: %w", err)
	}

	annotations := existing.Metadata.Annotations
	ready := existing.Status != nil && existing.Status.ReadyReplicas > 0

	return rolloutState{
		Current:   annotations[annotationRelease],
		LastGood:  annotations[annotationLastGood],
		Ready:     ready,
		StartedAt: parseTime(annotations[annotationRolloutTime]),
		Exists:    true,
	}, nil
}

// RolloutDecision is what the operator intends to do for one deployment this pass.
type RolloutDecision struct {
	// Release to configure. Differs from the declared one only when rolling back.
	Release string
	// RolledBack is set when the declared release was abandoned for the last good one.
	RolledBack bool
	// Deferred is set when the change is postponed because too many hosts are already
	// mid-change on this stamp.
	Deferred bool
	// Reason explains the decision, and becomes the condition reason.
	Reason string
}

// decideRollout chooses this pass's release for one deployment.
//
// Pure, so the policy can be tested without a cluster: the interesting cases are a new
// release that stalls, one that has never been ready, and a stamp already busy with
// another change.
func decideRollout(
	policy Rollout,
	item ModelDeployment,
	state rolloutState,
	inFlight int,
	now time.Time,
) RolloutDecision {
	declared := releaseOf(item)

	if !state.Exists {
		return RolloutDecision{Release: declared, Reason: "InitialRollout"}
	}

	if state.Current == declared {
		if state.Ready {
			return RolloutDecision{Release: declared, Reason: "Serving"}
		}
		// Already rolling out this release. Whether to give up depends on how long it
		// has had and whether there is anything to fall back to.
		elapsed := now.Sub(state.StartedAt)
		if policy.AutoRollback && state.LastGood != "" && state.LastGood != declared &&
			!state.StartedAt.IsZero() && elapsed > policy.ReadyTimeout {
			return RolloutDecision{
				Release:    state.LastGood,
				RolledBack: true,
				// Named for what happened rather than "Failed": the release may be
				// fine and the cluster short of capacity, and the operator cannot tell
				// those apart from readiness alone.
				Reason: "RolledBackAfterReadyTimeout",
			}
		}
		return RolloutDecision{Release: declared, Reason: "Progressing"}
	}

	// A different release is declared. Changing it stops the current server, so the
	// stamp's budget for simultaneous changes applies.
	if policy.MaxParallel > 0 && inFlight >= policy.MaxParallel {
		return RolloutDecision{
			Release:  state.Current,
			Deferred: true,
			Reason:   "DeferredWhileAnotherRolloutIsInFlight",
		}
	}
	return RolloutDecision{Release: declared, Reason: "RollingOut"}
}

// rolloutAnnotations records the decision on the workload so the next pass, in this
// process or another, can see the same history.
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
