package operator

import (
	"testing"
	"time"
)

func declared(release string) ModelDeployment {
	item := resource("alpha", "dep-a", "acct-a", "alpha-model", 1)
	item.Spec.UpstreamModel = release
	return item
}

func TestAFirstRolloutJustProceeds(t *testing.T) {
	decision := decideRollout(DefaultRollout(), declared("r1"), rolloutState{}, 0, time.Now())

	if decision.Release != "r1" || decision.RolledBack || decision.Deferred {
		t.Fatalf("unexpected decision: %+v", decision)
	}
	if decision.Reason != "InitialRollout" {
		t.Fatalf("reason = %q", decision.Reason)
	}
}

func TestAServingReleaseIsLeftAlone(t *testing.T) {
	state := rolloutState{Exists: true, Current: "r1", LastGood: "r1", Ready: true}

	decision := decideRollout(DefaultRollout(), declared("r1"), state, 0, time.Now())

	if decision.Reason != "Serving" {
		t.Fatalf("a healthy host was disturbed: %+v", decision)
	}
}

func TestAStartingReleaseIsGivenTime(t *testing.T) {
	// Weight loading takes minutes, so a host that is not ready yet is not failing.
	now := time.Now()
	state := rolloutState{
		Exists: true, Current: "r2", LastGood: "r1", Ready: false,
		StartedAt: now.Add(-2 * time.Minute),
	}

	decision := decideRollout(DefaultRollout(), declared("r2"), state, 0, now)

	if decision.RolledBack {
		t.Fatal("rolled back a release that was still within its deadline")
	}
	if decision.Reason != "Progressing" {
		t.Fatalf("reason = %q", decision.Reason)
	}
}

func TestAStalledReleaseIsRolledBack(t *testing.T) {
	now := time.Now()
	policy := DefaultRollout()
	state := rolloutState{
		Exists: true, Current: "r2", LastGood: "r1", Ready: false,
		StartedAt: now.Add(-policy.ReadyTimeout - time.Minute),
	}

	decision := decideRollout(policy, declared("r2"), state, 0, now)

	if !decision.RolledBack || decision.Release != "r1" {
		t.Fatalf("expected a rollback to r1: %+v", decision)
	}
	// Named for what happened: the release may be fine and the cluster short of
	// capacity, and readiness alone cannot tell those apart.
	if decision.Reason != "RolledBackAfterReadyTimeout" {
		t.Fatalf("reason = %q", decision.Reason)
	}
}

func TestWithNoGoodReleaseThereIsNothingToRollBackTo(t *testing.T) {
	// A first release that never becomes ready must keep trying: rolling back to
	// nothing would delete the only deployment the stamp has.
	now := time.Now()
	policy := DefaultRollout()
	state := rolloutState{
		Exists: true, Current: "r1", LastGood: "", Ready: false,
		StartedAt: now.Add(-policy.ReadyTimeout - time.Hour),
	}

	decision := decideRollout(policy, declared("r1"), state, 0, now)

	if decision.RolledBack {
		t.Fatal("rolled back to a release that was never known good")
	}
}

func TestRollbackCanBeDisabled(t *testing.T) {
	now := time.Now()
	policy := DefaultRollout()
	policy.AutoRollback = false
	state := rolloutState{
		Exists: true, Current: "r2", LastGood: "r1", Ready: false,
		StartedAt: now.Add(-policy.ReadyTimeout - time.Minute),
	}

	if decideRollout(policy, declared("r2"), state, 0, now).RolledBack {
		t.Fatal("rolled back with automatic rollback disabled")
	}
}

func TestOnlyOneReleaseChangesAtATime(t *testing.T) {
	// A bad release should cost one deployment, not every deployment on the stamp.
	state := rolloutState{Exists: true, Current: "r1", LastGood: "r1", Ready: true}

	decision := decideRollout(DefaultRollout(), declared("r2"), state, 1, time.Now())

	if !decision.Deferred {
		t.Fatalf("a second simultaneous rollout was allowed: %+v", decision)
	}
	// The running release stays configured while the change waits.
	if decision.Release != "r1" {
		t.Fatalf("deferring changed the release anyway: %+v", decision)
	}
}

func TestParallelismIsConfigurable(t *testing.T) {
	policy := DefaultRollout()
	policy.MaxParallel = 2
	state := rolloutState{Exists: true, Current: "r1", LastGood: "r1", Ready: true}

	if decideRollout(policy, declared("r2"), state, 1, time.Now()).Deferred {
		t.Fatal("deferred below the configured parallelism")
	}
}

func TestOnlyAReadyReleaseBecomesTheFallback(t *testing.T) {
	// Recording the declared release optimistically would let a broken one become the
	// thing rolled back to, which is worse than having no fallback.
	reconciler := New(nil, Options{Namespace: namespace, Log: discardLogger()})
	now := time.Now()

	unready := reconciler.rolloutAnnotations(
		RolloutDecision{Release: "r2"},
		rolloutState{Exists: true, Current: "r2", LastGood: "r1", Ready: false},
		now,
	)
	if unready[annotationLastGood] != "r1" {
		t.Fatalf("an unready release became the fallback: %v", unready)
	}

	ready := reconciler.rolloutAnnotations(
		RolloutDecision{Release: "r2"},
		rolloutState{Exists: true, Current: "r2", LastGood: "r1", Ready: true},
		now,
	)
	if ready[annotationLastGood] != "r2" {
		t.Fatalf("a ready release did not become the fallback: %v", ready)
	}
}

func TestTheRolloutClockResetsOnlyWhenTheReleaseChanges(t *testing.T) {
	// Otherwise a release would get a fresh deadline on every pass and never time out.
	reconciler := New(nil, Options{Namespace: namespace, Log: discardLogger()})
	started := time.Now().Add(-5 * time.Minute)

	unchanged := reconciler.rolloutAnnotations(
		RolloutDecision{Release: "r2"},
		rolloutState{Exists: true, Current: "r2", StartedAt: started},
		time.Now(),
	)
	if parseTime(unchanged[annotationRolloutTime]).Unix() != started.UTC().Unix() {
		t.Fatalf("the deadline was reset for an unchanged release: %v", unchanged)
	}

	changed := reconciler.rolloutAnnotations(
		RolloutDecision{Release: "r3"},
		rolloutState{Exists: true, Current: "r2", StartedAt: started},
		time.Now(),
	)
	if parseTime(changed[annotationRolloutTime]).Unix() == started.UTC().Unix() {
		t.Fatal("the deadline was not reset for a new release")
	}
}

// weightOf returns the weight a decision assigns a release, and whether it appears at all.
func weightOf(decision RolloutDecision, release string) (float64, bool) {
	for _, rw := range decision.Weighted {
		if rw.Release == release {
			return rw.Weight, true
		}
	}
	return 0, false
}

// coexisting is a stamp with room for two releases at once.
func coexisting(state rolloutState) rolloutState {
	state.CanCoexist = true
	if state.DesiredReplicas == 0 {
		state.DesiredReplicas = 1
	}
	return state
}

func TestAServingReleaseCarriesAllItsWeight(t *testing.T) {
	// The steady state is one release taking all of the traffic; the weighted set exists
	// so the data plane always has a weight to read, even when there is only one.
	state := rolloutState{Exists: true, Current: "r1", LastGood: "r1", Ready: true}
	decision := decideRollout(DefaultRollout(), declared("r1"), coexisting(state), 0, time.Now())

	if len(decision.Weighted) != 1 {
		t.Fatalf("a single release should carry one weight: %+v", decision.Weighted)
	}
	if w, _ := weightOf(decision, "r1"); w != 1.0 {
		t.Fatalf("the serving release does not carry all the weight: %+v", decision.Weighted)
	}
	if decision.Coexist {
		t.Fatal("a steady deployment must not run a sidecar")
	}
}

func TestAReleaseChangeStartsASidecarAndKeepsAllTrafficOnTheOld(t *testing.T) {
	// The instant a change is declared the new release has no ready pod, so every request
	// must still go to the old release, which keeps serving from its sidecar.
	state := coexisting(rolloutState{
		Exists: true, Current: "r1", LastGood: "r1", Ready: true, ReadyReplicas: 1,
		DesiredReplicas: 1,
	})
	decision := decideRollout(DefaultRollout(), declared("r2"), state, 0, time.Now())

	if !decision.Coexist || decision.SidecarRelease != "r1" {
		t.Fatalf("the old release was not kept beside the new one: %+v", decision)
	}
	if w, _ := weightOf(decision, "r1"); w != 1.0 {
		t.Fatalf("traffic left the old release before the new one was ready: %+v", decision.Weighted)
	}
	if _, present := weightOf(decision, "r2"); present {
		if w, _ := weightOf(decision, "r2"); w != 0 {
			t.Fatalf("the not-yet-ready release was given traffic: %+v", decision.Weighted)
		}
	}
}

func TestWeightShiftsToTheNewReleaseAsItsPodsBecomeReady(t *testing.T) {
	// The heart of a downtime-free rollout: as the new release's replicas come ready the
	// weight moves to it in proportion, so the old release drains rather than being cut.
	policy := DefaultRollout()
	now := time.Now()
	// Four desired replicas, two of the new release ready: an even split.
	state := coexisting(rolloutState{
		Exists: true, Current: "r2", LastGood: "r1", Ready: false, ReadyReplicas: 2,
		DesiredReplicas: 4, PrevRelease: "r1", SidecarExists: true, SidecarReady: true,
		StartedAt: now.Add(-time.Minute),
	})
	decision := decideRollout(policy, declared("r2"), state, 0, now)

	if !decision.Coexist {
		t.Fatalf("the shift is not coexisting: %+v", decision)
	}
	newWeight, ok := weightOf(decision, "r2")
	if !ok || newWeight != 0.5 {
		t.Fatalf("new release weight = %v, want 0.5: %+v", newWeight, decision.Weighted)
	}
	oldWeight, _ := weightOf(decision, "r1")
	if oldWeight != 0.5 {
		t.Fatalf("old release weight = %v, want 0.5: %+v", oldWeight, decision.Weighted)
	}
	// The two weights cover all the traffic, so nothing is dropped mid-shift.
	if newWeight+oldWeight != 1.0 {
		t.Fatalf("weights do not sum to 1, traffic is dropped: %+v", decision.Weighted)
	}
}

func TestTheOldReleaseIsRemovedWhenTheNewIsFullyReady(t *testing.T) {
	// Once the new release can carry everything the sidecar is drained and removed, and
	// the config returns to a single release taking all the traffic.
	state := coexisting(rolloutState{
		Exists: true, Current: "r2", LastGood: "r2", Ready: true, ReadyReplicas: 2,
		DesiredReplicas: 2, PrevRelease: "r1", SidecarExists: true, SidecarReady: true,
	})
	decision := decideRollout(DefaultRollout(), declared("r2"), state, 0, time.Now())

	if !decision.RemoveSidecar || decision.SidecarRelease != "r1" {
		t.Fatalf("the old release was not removed once the new was ready: %+v", decision)
	}
	if len(decision.Weighted) != 1 {
		t.Fatalf("the config did not return to a single release: %+v", decision.Weighted)
	}
	if w, _ := weightOf(decision, "r2"); w != 1.0 {
		t.Fatalf("the new release does not carry all the traffic: %+v", decision.Weighted)
	}
	if _, present := weightOf(decision, "r1"); present {
		t.Fatalf("the drained release still receives traffic: %+v", decision.Weighted)
	}
}

func TestAStalledNewReleaseRollsBackKeepingTrafficOnTheLastGood(t *testing.T) {
	// The safety property: a new release that never becomes ready must not drop a single
	// request. The old release has been serving from its sidecar throughout, so rollback
	// returns all the weight to it and the failed new release is abandoned.
	policy := DefaultRollout()
	now := time.Now()
	state := coexisting(rolloutState{
		Exists: true, Current: "r2", LastGood: "r1", Ready: false, ReadyReplicas: 0,
		DesiredReplicas: 1, PrevRelease: "r1", SidecarExists: true, SidecarReady: true,
		StartedAt: now.Add(-policy.ReadyTimeout - time.Minute),
	})
	decision := decideRollout(policy, declared("r2"), state, 0, now)

	if !decision.RolledBack {
		t.Fatalf("a stalled new release was not rolled back: %+v", decision)
	}
	if decision.Release != "r1" {
		t.Fatalf("rollback did not return to the last-good release: %+v", decision)
	}
	// Every unit of weight is on a release that is actually serving: the pool is never
	// empty, so no request is dropped.
	total := 0.0
	for _, rw := range decision.Weighted {
		total += rw.Weight
		if rw.Release == "r2" && rw.Weight > 0 {
			t.Fatalf("the failed release still receives traffic: %+v", decision.Weighted)
		}
	}
	if total <= 0 {
		t.Fatalf("rollback left the pool empty, dropping requests: %+v", decision.Weighted)
	}
	if w, _ := weightOf(decision, "r1"); w != 1.0 {
		t.Fatalf("the last-good release does not carry all the traffic: %+v", decision.Weighted)
	}
}

func TestARolloutDefersToRecreateWhenTheStampCannotFitBoth(t *testing.T) {
	// Honesty about the hardware: a GPU is not shared, so where the stamp has no spare
	// device the two releases cannot coexist and the operator falls back to Recreate
	// rather than deadlocking on a device that will never free.
	state := rolloutState{
		Exists: true, Current: "r1", LastGood: "r1", Ready: true, ReadyReplicas: 1,
		DesiredReplicas: 1, CanCoexist: false,
	}
	decision := decideRollout(DefaultRollout(), declared("r2"), state, 0, time.Now())

	if decision.Coexist {
		t.Fatalf("a stamp with no spare GPU pretended to coexist: %+v", decision)
	}
	if decision.Reason != "RollingOutRecreate" {
		t.Fatalf("reason = %q, want RollingOutRecreate", decision.Reason)
	}
	if decision.Release != "r2" {
		t.Fatalf("the Recreate path did not move to the declared release: %+v", decision)
	}
	// A single release, since one device serves one server at a time.
	if len(decision.Weighted) != 1 {
		t.Fatalf("the Recreate path split traffic it cannot split: %+v", decision.Weighted)
	}
}

func TestAWeightedRolloutRecordsTheSidecarReleaseInAnnotations(t *testing.T) {
	// The next reconcile, in this process or another, must know which release the sidecar
	// carries, so it is recorded on the primary workload rather than in operator memory.
	reconciler := New(nil, Options{Namespace: namespace, Log: discardLogger()})
	decision := RolloutDecision{
		Release: "r2", Coexist: true, SidecarRelease: "r1",
		Weighted: []ReleaseWeight{{Release: "r1", Weight: 1.0}},
	}
	annotations := reconciler.rolloutAnnotations(
		decision, rolloutState{Exists: true, Current: "r1"}, time.Now(),
	)
	if annotations[annotationRollingFrom] != "r1" {
		t.Fatalf("the sidecar release was not recorded: %v", annotations)
	}

	// Once the sidecar is removed the marker is cleared, so a settled deployment carries
	// no stale rollout state.
	done := reconciler.rolloutAnnotations(
		RolloutDecision{Release: "r2", Coexist: true, SidecarRelease: "r1", RemoveSidecar: true},
		rolloutState{Exists: true, Current: "r2", Ready: true},
		time.Now(),
	)
	if _, present := done[annotationRollingFrom]; present {
		t.Fatalf("a completed rollout left a stale sidecar marker: %v", done)
	}
}
