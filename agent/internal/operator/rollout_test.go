package operator

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

func declared(release string) ModelDeployment {
	item := resource("alpha", "dep-a", "acct-a", "alpha-model", 1)
	item.Spec.UpstreamModel = release
	return item
}

func servingStatus(release, workload string, generation int64) *RolloutStatus {
	return &RolloutStatus{
		Phase: rolloutPhaseServing, TargetGeneration: generation,
		ActiveRelease: release, ActiveWorkload: workload,
	}
}

func TestInitialReleaseUsesStableWorkload(t *testing.T) {
	item := declared("r1")
	decision := decideRollout(DefaultRollout(), item, rolloutState{}, 0, time.Now())
	if decision.Phase != rolloutPhasePreparing || decision.ActiveWorkload != hostName(item) ||
		!decision.EnsureActive || decision.EnsureCandidate {
		t.Fatalf("initial decision = %+v", decision)
	}
}

func TestServingReleaseIsLeftAlone(t *testing.T) {
	item := declared("r1")
	state := rolloutState{Status: servingStatus("r1", hostName(item), 1)}
	decision := decideRollout(DefaultRollout(), item, state, 0, time.Now())
	if decision.Phase != rolloutPhaseServing || decision.ActiveRelease != "r1" {
		t.Fatalf("serving decision = %+v", decision)
	}
}

func TestReleaseChangePreparesCandidateWithoutMutatingActive(t *testing.T) {
	item := declared("r2")
	state := rolloutState{Status: servingStatus("r1", hostName(item), 1)}
	decision := decideRollout(DefaultRollout(), item, state, 0, time.Now())
	if decision.Phase != rolloutPhasePreparing || decision.ActiveRelease != "r1" ||
		decision.ActiveWorkload != hostName(item) || decision.CandidateRelease != "r2" ||
		decision.CandidateWorkload == hostName(item) || !decision.EnsureCandidate {
		t.Fatalf("candidate decision = %+v", decision)
	}
	if len(decision.Weighted) != 1 || decision.Weighted[0].Release != "r1" ||
		decision.Weighted[0].Weight != 1 {
		t.Fatalf("active route changed during preparation: %+v", decision.Weighted)
	}
}

func TestPartialCandidateReadinessKeepsAllTrafficOnActive(t *testing.T) {
	item := declared("r2")
	status := &RolloutStatus{
		Phase: rolloutPhasePreparing, TargetGeneration: item.Metadata.Generation,
		ActiveRelease: "r1", ActiveWorkload: hostName(item),
		CandidateRelease: "r2", CandidateWorkload: hostNameForRelease(item, "r2"),
		StartedAt: time.Now().UTC().Format(time.RFC3339),
	}
	state := rolloutState{
		Status: status, CandidateExists: true,
		CandidateReadiness: modelHostReadiness{Ready: 1, Desired: 2},
		CandidateConcrete:  true,
	}
	decision := decideRollout(DefaultRollout(), item, state, 0, time.Now())
	if decision.Phase != rolloutPhasePreparing || len(decision.Weighted) != 1 ||
		decision.Weighted[0].Release != "r1" {
		t.Fatalf("partial candidate received traffic: %+v", decision)
	}
}

func TestFullyReadyConcreteCandidateCutsOverWithExplicitZero(t *testing.T) {
	item := declared("r2")
	status := &RolloutStatus{
		Phase: rolloutPhasePreparing, TargetGeneration: item.Metadata.Generation,
		ActiveRelease: "r1", ActiveWorkload: hostName(item),
		CandidateRelease: "r2", CandidateWorkload: hostNameForRelease(item, "r2"),
		StartedAt: time.Now().UTC().Format(time.RFC3339),
	}
	state := rolloutState{
		Status: status, CandidateExists: true,
		CandidateReadiness: modelHostReadiness{Ready: 2, Desired: 2},
		CandidateConcrete:  true,
	}
	decision := decideRollout(DefaultRollout(), item, state, 0, time.Now())
	if decision.Phase != rolloutPhaseDraining || !decision.ForceWeighted ||
		len(decision.Weighted) != 2 || decision.Weighted[0].Weight != 0 ||
		decision.Weighted[1].Weight != 1 {
		t.Fatalf("cutover decision = %+v", decision)
	}
}

func TestServiceFallbackDoesNotPromoteCandidate(t *testing.T) {
	item := declared("r2")
	status := &RolloutStatus{
		Phase: rolloutPhasePreparing, TargetGeneration: item.Metadata.Generation,
		ActiveRelease: "r1", ActiveWorkload: hostName(item),
		CandidateRelease: "r2", CandidateWorkload: hostNameForRelease(item, "r2"),
		StartedAt: time.Now().UTC().Format(time.RFC3339),
	}
	state := rolloutState{
		Status:             status,
		CandidateReadiness: modelHostReadiness{Ready: 1, Desired: 1},
		CandidateConcrete:  false,
	}
	if got := decideRollout(DefaultRollout(), item, state, 0, time.Now()); got.Phase != rolloutPhasePreparing {
		t.Fatalf("DNS fallback promoted candidate: %+v", got)
	}
}

func TestDrainAcknowledgementPromotesAndDeletesOldActive(t *testing.T) {
	item := declared("r2")
	status := &RolloutStatus{
		Phase: rolloutPhaseDraining, TargetGeneration: item.Metadata.Generation,
		ActiveRelease: "r1", ActiveWorkload: hostName(item),
		CandidateRelease: "r2", CandidateWorkload: hostNameForRelease(item, "r2"),
		CutoverRevision: "rev", DrainingBackendIDs: []string{"old"},
	}
	decision := decideRollout(DefaultRollout(), item,
		rolloutState{
			Status: status, DrainAcknowledged: true,
			CandidateReadiness: modelHostReadiness{Ready: 1, Desired: 1},
		}, 0, time.Now())
	if decision.Phase != rolloutPhaseServing || !decision.DeleteActive ||
		decision.ActiveRelease != "r2" || decision.ActiveWorkload != status.CandidateWorkload {
		t.Fatalf("promotion decision = %+v", decision)
	}
}

func TestCandidateTimeoutKeepsActiveAndMarksGenerationFailed(t *testing.T) {
	item := declared("r2")
	policy := DefaultRollout()
	status := &RolloutStatus{
		Phase: rolloutPhasePreparing, TargetGeneration: item.Metadata.Generation,
		ActiveRelease: "r1", ActiveWorkload: hostName(item),
		CandidateRelease: "r2", CandidateWorkload: hostNameForRelease(item, "r2"),
		StartedAt: time.Now().Add(-policy.ReadyTimeout - time.Minute).UTC().Format(time.RFC3339),
	}
	decision := decideRollout(policy, item, rolloutState{Status: status}, 0, time.Now())
	if decision.Phase != rolloutPhaseFailed || !decision.DeleteCandidate ||
		decision.ActiveRelease != "r1" || decision.FailedGeneration != item.Metadata.Generation {
		t.Fatalf("timeout decision = %+v", decision)
	}
}

func TestPersistedRolloutConsumesParallelBudgetAfterRestart(t *testing.T) {
	item := declared("r2")
	state := rolloutState{Status: servingStatus("r1", hostName(item), 1)}
	decision := decideRollout(DefaultRollout(), item, state, 1, time.Now())
	if !decision.Deferred || decision.ActiveRelease != "r1" {
		t.Fatalf("parallel rollout was not deferred: %+v", decision)
	}
}

func TestRouterDrainRequiresMatchingRevisionAndZeroOldInflight(t *testing.T) {
	state := `{"revision":"global","backends":[{"deployment_id":"dep-a","backend_id":"old","route_revision":"cutover","workload":"active","in_flight":1}]}`
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte(state))
	}))
	defer server.Close()
	reconciler := New(nil, Options{RouterStatusURL: server.URL, RouterHTTP: server.Client()})

	drained, err := reconciler.routerDrained(context.Background(), "dep-a", "cutover", "active")
	if err != nil || drained {
		t.Fatalf("active old request reported drained: drained=%v err=%v", drained, err)
	}
	state = `{"revision":"global","backends":[{"deployment_id":"dep-a","backend_id":"old","route_revision":"other","workload":"active","in_flight":0}]}`
	drained, err = reconciler.routerDrained(context.Background(), "dep-a", "cutover", "active")
	if err != nil || drained {
		t.Fatalf("stale revision reported drained: drained=%v err=%v", drained, err)
	}
	state = `{"revision":"global","backends":[{"deployment_id":"dep-a","backend_id":"old","route_revision":"cutover","workload":"active","in_flight":0}]}`
	drained, err = reconciler.routerDrained(context.Background(), "dep-a", "cutover", "active")
	if err != nil || !drained {
		t.Fatalf("matching zero-inflight route did not drain: drained=%v err=%v", drained, err)
	}
}

func TestCandidateFailureAfterCutoverReversesWeightsBeforeDeletion(t *testing.T) {
	item := declared("r2")
	status := &RolloutStatus{
		Phase: rolloutPhaseDraining, TargetGeneration: item.Metadata.Generation,
		ActiveRelease: "r1", ActiveWorkload: hostName(item),
		CandidateRelease: "r2", CandidateWorkload: hostNameForRelease(item, "r2"),
		CutoverRevision: "new-positive", DrainingBackendIDs: []string{"old"},
	}
	decision := decideRollout(DefaultRollout(), item, rolloutState{
		Status: status, DrainAcknowledged: true,
		CandidateReadiness: modelHostReadiness{Ready: 0, Desired: 1},
	}, 0, time.Now())
	if !decision.RollbackDrain || decision.DeleteCandidate || decision.CutoverRevision != "" ||
		len(decision.Weighted) != 2 || decision.Weighted[0].Weight != 1 ||
		decision.Weighted[1].Weight != 0 {
		t.Fatalf("rollback did not reverse route before cleanup: %+v", decision)
	}
}

func TestAcknowledgedReverseDrainMarksGenerationFailedAndDoesNotRetry(t *testing.T) {
	item := declared("r2")
	status := &RolloutStatus{
		Phase: rolloutPhaseDraining, TargetGeneration: item.Metadata.Generation,
		ActiveRelease: "r1", ActiveWorkload: hostName(item),
		CandidateRelease: "r2", CandidateWorkload: hostNameForRelease(item, "r2"),
		CutoverRevision: "rollback-route", DrainingBackendIDs: []string{"new"},
		Rollback: true,
	}
	decision := decideRollout(DefaultRollout(), item, rolloutState{
		Status: status, DrainAcknowledged: true,
	}, 0, time.Now())
	if decision.Phase != rolloutPhaseFailed || decision.FailedGeneration != item.Metadata.Generation ||
		!decision.DeleteCandidate || decision.ActiveRelease != "r1" {
		t.Fatalf("acknowledged rollback = %+v", decision)
	}
	persisted := rolloutStatusFromDecision(item, decision, "active-only", nil)
	next := decideRollout(DefaultRollout(), item, rolloutState{Status: persisted}, 0, time.Now())
	if next.EnsureCandidate || next.CandidateRelease != "" || next.Phase != rolloutPhaseFailed {
		t.Fatalf("failed generation retried immediately: %+v", next)
	}
}

func TestRevertingDesiredReleaseCancelsCandidateInsteadOfDuplicatingActive(t *testing.T) {
	item := declared("r1")
	status := &RolloutStatus{
		Phase: rolloutPhasePreparing, TargetGeneration: item.Metadata.Generation - 1,
		ActiveRelease: "r1", ActiveWorkload: hostName(item),
		CandidateRelease: "r2", CandidateWorkload: hostNameForRelease(item, "r2"),
	}
	decision := decideRollout(DefaultRollout(), item, rolloutState{Status: status}, 0, time.Now())
	if decision.Phase != rolloutPhaseServing || !decision.DeleteCandidate ||
		decision.EnsureCandidate || decision.ActiveWorkload != hostName(item) {
		t.Fatalf("revert created a redundant active-release candidate: %+v", decision)
	}
}

func TestNewGenerationDuringDrainRollsBackOldCandidateThenPreparesNewRelease(t *testing.T) {
	item := declared("r3")
	item.Metadata.Generation = 3
	status := &RolloutStatus{
		Phase: rolloutPhaseDraining, TargetGeneration: 2,
		ActiveRelease: "r1", ActiveWorkload: hostName(item),
		CandidateRelease: "r2", CandidateWorkload: hostNameForRelease(item, "r2"),
		CutoverRevision: "r2-positive", DrainingBackendIDs: []string{"old"},
	}
	first := decideRollout(DefaultRollout(), item, rolloutState{
		Status: status, DrainAcknowledged: true,
		CandidateReadiness: modelHostReadiness{Ready: 1, Desired: 1},
	}, 0, time.Now())
	if !first.RollbackDrain || first.DeleteCandidate || first.CutoverRevision != "" {
		t.Fatalf("new generation reused forward ack instead of reversing route: %+v", first)
	}
	reverse := rolloutStatusFromDecision(item, first, "r1-positive", []string{"r2-pod"})
	second := decideRollout(DefaultRollout(), item, rolloutState{
		Status: reverse, DrainAcknowledged: true,
	}, 0, time.Now())
	if second.Phase != rolloutPhaseFailed || second.FailedGeneration != 2 {
		t.Fatalf("rollback failed the unattempted generation: %+v", second)
	}
	failed := rolloutStatusFromDecision(item, second, "r1-only", nil)
	third := decideRollout(DefaultRollout(), item, rolloutState{Status: failed}, 0, time.Now())
	if third.Phase != rolloutPhasePreparing || third.CandidateRelease != "r3" {
		t.Fatalf("new declaration was suppressed after rollback: %+v", third)
	}
}
