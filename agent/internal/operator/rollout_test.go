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
