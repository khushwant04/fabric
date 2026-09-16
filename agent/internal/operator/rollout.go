package operator

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"strings"
	"time"
)

// Rollout controls readiness timeout and stamp-wide rollout concurrency.
type Rollout struct {
	ReadyTimeout time.Duration
	MaxParallel  int
	AutoRollback bool
}

func DefaultRollout() Rollout {
	return Rollout{ReadyTimeout: 15 * time.Minute, MaxParallel: 1, AutoRollback: true}
}

const (
	ConditionProgressing = "Progressing"
	ConditionAvailable   = "Available"

	rolloutPhaseServing   = "Serving"
	rolloutPhasePreparing = "Preparing"
	rolloutPhaseDraining  = "Draining"
	rolloutPhaseFailed    = "Failed"

	annotationRelease = "fabric.khushwant.dev/release"
)

func releaseOf(item ModelDeployment) string {
	if item.Spec.UpstreamModel != "" {
		return item.Spec.UpstreamModel
	}
	return item.Spec.ModelAlias
}

// RolloutStatus is the durable checkpoint stored on the CR status subresource.
// Deterministic workload names and the route document remain reconstructable truth if
// a status write fails; this records which phase may perform destructive cleanup.
type RolloutStatus struct {
	Phase              string   `json:"phase,omitempty"`
	TargetGeneration   int64    `json:"targetGeneration,omitempty"`
	ActiveRelease      string   `json:"activeRelease,omitempty"`
	ActiveWorkload     string   `json:"activeWorkload,omitempty"`
	CandidateRelease   string   `json:"candidateRelease,omitempty"`
	CandidateWorkload  string   `json:"candidateWorkload,omitempty"`
	StartedAt          string   `json:"startedAt,omitempty"`
	CutoverRevision    string   `json:"cutoverRevision,omitempty"`
	DrainingBackendIDs []string `json:"drainingBackendIds,omitempty"`
	DrainingWorkload   string   `json:"drainingWorkload,omitempty"`
	FailedGeneration   int64    `json:"failedGeneration,omitempty"`
	Rollback           bool     `json:"rollback,omitempty"`
}

func (s *RolloutStatus) inProgress() bool {
	return s != nil && (s.Phase == rolloutPhasePreparing || s.Phase == rolloutPhaseDraining)
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

// rolloutState is the pure policy input assembled from CR status and live workloads.
type rolloutState struct {
	Status             *RolloutStatus
	ActiveExists       bool
	ActiveReadiness    modelHostReadiness
	CandidateExists    bool
	CandidateReadiness modelHostReadiness
	CandidateConcrete  bool
	DrainAcknowledged  bool
}

// ReleaseWeight is a release's total relative traffic share. The operator divides this
// by that release's concrete ready endpoints before writing backend weights.
type ReleaseWeight struct {
	Release  string
	Workload string
	Weight   float64
}

// RolloutDecision is a non-destructive plan for one reconcile pass.
type RolloutDecision struct {
	Phase              string
	TargetGeneration   int64
	Reason             string
	Deferred           bool
	RolledBack         bool
	ActiveRelease      string
	ActiveWorkload     string
	CandidateRelease   string
	CandidateWorkload  string
	EnsureActive       bool
	EnsureCandidate    bool
	DeleteActive       bool
	DeleteCandidate    bool
	Weighted           []ReleaseWeight
	ForceWeighted      bool
	StartedAt          string
	FailedGeneration   int64
	RollbackDrain      bool
	CutoverRevision    string
	DrainingBackendIDs []string
	DrainingWorkload   string
}

func releaseWorkloadName(item ModelDeployment, release string) string {
	return hostNameForRelease(item, release)
}

func servingDecision(activeRelease, activeWorkload, reason string) RolloutDecision {
	return RolloutDecision{
		Phase:          rolloutPhaseServing,
		Reason:         reason,
		ActiveRelease:  activeRelease,
		ActiveWorkload: activeWorkload,
		EnsureActive:   true,
		Weighted: []ReleaseWeight{{
			Release: activeRelease, Workload: activeWorkload, Weight: 1,
		}},
	}
}

// decideRollout never mutates a live workload. A new release is always a distinct,
// deterministic candidate; the active route remains until the candidate is fully ready.
func decideRollout(
	policy Rollout, item ModelDeployment, state rolloutState, inFlight int, now time.Time,
) RolloutDecision {
	declared := releaseOf(item)
	generation := item.Metadata.Generation

	if state.Status == nil {
		// Upgrade compatibility: an existing stable workload becomes the durable active
		// workload. A first release uses the same stable name.
		active := hostName(item)
		if state.ActiveExists {
			return servingDecision(declared, active, "Serving")
		}
		decision := servingDecision(declared, active, "InitialRollout")
		decision.Phase = rolloutPhasePreparing
		decision.StartedAt = now.UTC().Format(time.RFC3339)
		return decision
	}

	status := state.Status
	if status.FailedGeneration == generation && status.Phase == rolloutPhaseFailed {
		decision := servingDecision(status.ActiveRelease, status.ActiveWorkload, "RolloutFailed")
		decision.Phase = rolloutPhaseFailed
		decision.FailedGeneration = generation
		decision.RolledBack = status.ActiveRelease != ""
		return decision
	}
	if status.Phase == rolloutPhaseFailed && status.FailedGeneration != generation {
		recovered := *status
		recovered.Phase = rolloutPhaseServing
		recovered.FailedGeneration = 0
		state.Status = &recovered
		return decideRollout(policy, item, state, inFlight, now)
	}

	if status.Phase == rolloutPhaseServing && status.ActiveRelease == declared {
		return servingDecision(status.ActiveRelease, status.ActiveWorkload, "Serving")
	}

	if status.Phase == rolloutPhaseServing && status.ActiveRelease != declared {
		if policy.MaxParallel > 0 && inFlight >= policy.MaxParallel {
			decision := servingDecision(status.ActiveRelease, status.ActiveWorkload,
				"DeferredWhileAnotherRolloutIsInFlight")
			decision.Deferred = true
			return decision
		}
		candidate := releaseWorkloadName(item, declared)
		return RolloutDecision{
			Phase: rolloutPhasePreparing, Reason: "PreparingCandidate",
			ActiveRelease: status.ActiveRelease, ActiveWorkload: status.ActiveWorkload,
			CandidateRelease: declared, CandidateWorkload: candidate,
			EnsureActive: true, EnsureCandidate: true,
			StartedAt: now.UTC().Format(time.RFC3339),
			Weighted: []ReleaseWeight{{
				Release: status.ActiveRelease, Workload: status.ActiveWorkload, Weight: 1,
			}},
		}
	}

	if status.Phase == rolloutPhasePreparing && status.CandidateWorkload == "" {
		if state.ActiveReadiness.fullyReady() {
			return servingDecision(status.ActiveRelease, status.ActiveWorkload, "Serving")
		}
		started := parseTime(status.StartedAt)
		if !started.IsZero() && now.Sub(started) > policy.ReadyTimeout {
			return RolloutDecision{
				Phase: rolloutPhaseFailed, Reason: "InitialReleaseReadyTimeout",
				ActiveRelease: status.ActiveRelease, ActiveWorkload: status.ActiveWorkload,
				EnsureActive: true, FailedGeneration: generation,
				Weighted: []ReleaseWeight{{
					Release: status.ActiveRelease, Workload: status.ActiveWorkload, Weight: 1,
				}},
			}
		}
		return RolloutDecision{
			Phase: rolloutPhasePreparing, Reason: "InitialReleaseStarting",
			ActiveRelease: status.ActiveRelease, ActiveWorkload: status.ActiveWorkload,
			EnsureActive: true, StartedAt: status.StartedAt,
			Weighted: []ReleaseWeight{{
				Release: status.ActiveRelease, Workload: status.ActiveWorkload, Weight: 1,
			}},
		}
	}

	// A new generation while another candidate is preparing abandons only that candidate;
	// the active route was never changed.
	if status.Phase == rolloutPhasePreparing &&
		(status.TargetGeneration != generation || status.CandidateRelease != declared) {
		if declared == status.ActiveRelease {
			decision := servingDecision(status.ActiveRelease, status.ActiveWorkload,
				"CandidateCancelled")
			decision.DeleteCandidate = status.CandidateWorkload != ""
			decision.CandidateRelease = status.CandidateRelease
			decision.CandidateWorkload = status.CandidateWorkload
			return decision
		}
		candidate := releaseWorkloadName(item, declared)
		return RolloutDecision{
			Phase: rolloutPhasePreparing, Reason: "CandidateSuperseded",
			ActiveRelease: status.ActiveRelease, ActiveWorkload: status.ActiveWorkload,
			CandidateRelease: declared, CandidateWorkload: candidate,
			EnsureActive: true, EnsureCandidate: true,
			DeleteCandidate: status.CandidateWorkload != "" && status.CandidateWorkload != candidate,
			StartedAt:       now.UTC().Format(time.RFC3339),
			Weighted: []ReleaseWeight{{
				Release: status.ActiveRelease, Workload: status.ActiveWorkload, Weight: 1,
			}},
		}
	}

	if status.Phase == rolloutPhasePreparing {
		started := parseTime(status.StartedAt)
		if policy.AutoRollback && !started.IsZero() && now.Sub(started) > policy.ReadyTimeout {
			decision := servingDecision(status.ActiveRelease, status.ActiveWorkload,
				"RolledBackAfterReadyTimeout")
			decision.Phase = rolloutPhaseFailed
			decision.RolledBack = status.ActiveRelease != ""
			decision.DeleteCandidate = true
			decision.CandidateRelease = status.CandidateRelease
			decision.CandidateWorkload = status.CandidateWorkload
			decision.FailedGeneration = generation
			return decision
		}
		if state.CandidateReadiness.fullyReady() && state.CandidateConcrete {
			return RolloutDecision{
				Phase: rolloutPhaseDraining, Reason: "CutoverPublished",
				ActiveRelease: status.ActiveRelease, ActiveWorkload: status.ActiveWorkload,
				CandidateRelease:  status.CandidateRelease,
				CandidateWorkload: status.CandidateWorkload,
				EnsureActive:      true, EnsureCandidate: true, ForceWeighted: true,
				StartedAt: status.StartedAt, DrainingWorkload: status.ActiveWorkload,
				Weighted: []ReleaseWeight{
					{Release: status.ActiveRelease, Workload: status.ActiveWorkload, Weight: 0},
					{Release: status.CandidateRelease, Workload: status.CandidateWorkload, Weight: 1},
				},
			}
		}
		return RolloutDecision{
			Phase: rolloutPhasePreparing, Reason: "CandidateStarting",
			ActiveRelease: status.ActiveRelease, ActiveWorkload: status.ActiveWorkload,
			CandidateRelease:  status.CandidateRelease,
			CandidateWorkload: status.CandidateWorkload,
			EnsureActive:      true, EnsureCandidate: true, StartedAt: status.StartedAt,
			Weighted: []ReleaseWeight{{
				Release: status.ActiveRelease, Workload: status.ActiveWorkload, Weight: 1,
			}},
		}
	}

	if status.Phase == rolloutPhaseDraining {
		rollback := status.Rollback || status.TargetGeneration != generation ||
			status.CandidateRelease != declared || !state.CandidateReadiness.fullyReady()
		if state.DrainAcknowledged && (!rollback || status.Rollback) {
			if rollback {
				decision := servingDecision(status.ActiveRelease, status.ActiveWorkload,
					"RollbackComplete")
				decision.Phase = rolloutPhaseFailed
				decision.FailedGeneration = status.TargetGeneration
				decision.DeleteCandidate = true
				decision.CandidateRelease = status.CandidateRelease
				decision.CandidateWorkload = status.CandidateWorkload
				decision.RolledBack = true
				return decision
			}
			decision := servingDecision(status.CandidateRelease, status.CandidateWorkload,
				"RolloutComplete")
			decision.DeleteActive = true
			decision.ActiveRelease = status.CandidateRelease
			decision.ActiveWorkload = status.CandidateWorkload
			decision.CandidateRelease = ""
			decision.CandidateWorkload = ""
			return decision
		}
		decision := RolloutDecision{
			Phase: rolloutPhaseDraining, TargetGeneration: status.TargetGeneration,
			Reason:        "WaitingForDrain",
			ActiveRelease: status.ActiveRelease, ActiveWorkload: status.ActiveWorkload,
			CandidateRelease:  status.CandidateRelease,
			CandidateWorkload: status.CandidateWorkload,
			EnsureActive:      true, EnsureCandidate: true, ForceWeighted: true,
			StartedAt:          status.StartedAt,
			CutoverRevision:    status.CutoverRevision,
			DrainingBackendIDs: append([]string(nil), status.DrainingBackendIDs...),
			DrainingWorkload:   status.DrainingWorkload,
			RollbackDrain:      rollback,
		}
		if rollback {
			decision.Reason = "RollingBackCandidate"
			decision.RolledBack = true
			if !status.Rollback {
				// Direction changed: wait for acknowledgement of the newly published
				// active=1/candidate=0 revision, not the previous cutover revision.
				decision.CutoverRevision = ""
				decision.DrainingBackendIDs = nil
				decision.DrainingWorkload = status.CandidateWorkload
			}
			decision.Weighted = []ReleaseWeight{
				{Release: status.ActiveRelease, Workload: status.ActiveWorkload, Weight: 1},
				{Release: status.CandidateRelease, Workload: status.CandidateWorkload, Weight: 0},
			}
		} else {
			decision.Weighted = []ReleaseWeight{
				{Release: status.ActiveRelease, Workload: status.ActiveWorkload, Weight: 0},
				{Release: status.CandidateRelease, Workload: status.CandidateWorkload, Weight: 1},
			}
		}
		return decision
	}

	// Unknown/stale phase fails closed on the recorded active release.
	return servingDecision(status.ActiveRelease, status.ActiveWorkload, "RecoveredServingState")
}

type routerState struct {
	Revision string `json:"revision"`
	Backends []struct {
		DeploymentID  string `json:"deployment_id"`
		BackendID     string `json:"backend_id"`
		RouteRevision string `json:"route_revision"`
		Workload      string `json:"workload"`
		InFlight      int    `json:"in_flight"`
	} `json:"backends"`
}

func (r *Reconciler) routerDrained(
	ctx context.Context, deploymentID, revision, drainingWorkload string,
) (bool, error) {
	if revision == "" || drainingWorkload == "" || r.options.RouterStatusURL == "" {
		return false, nil
	}
	request, err := http.NewRequestWithContext(ctx, http.MethodGet,
		strings.TrimSuffix(r.options.RouterStatusURL, "/")+"/router-state", nil)
	if err != nil {
		return false, fmt.Errorf("build router status request: %w", err)
	}
	response, err := r.options.RouterHTTP.Do(request)
	if err != nil {
		return false, fmt.Errorf("read router status: %w", err)
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return false, fmt.Errorf("router status returned %s", response.Status)
	}
	var observed routerState
	if err := json.NewDecoder(response.Body).Decode(&observed); err != nil {
		return false, fmt.Errorf("decode router status: %w", err)
	}
	if observed.Revision == "" {
		return false, nil
	}
	matchedRevision := false
	for _, backend := range observed.Backends {
		if backend.DeploymentID != deploymentID {
			continue
		}
		if backend.RouteRevision != revision {
			return false, nil
		}
		matchedRevision = true
		if backend.Workload == drainingWorkload && backend.InFlight > 0 {
			return false, nil
		}
	}
	return matchedRevision, nil
}

func rolloutStatusFromDecision(
	item ModelDeployment, decision RolloutDecision, revision string, drainingIDs []string,
) *RolloutStatus {
	statusTarget := decision.TargetGeneration
	if statusTarget == 0 {
		statusTarget = item.Metadata.Generation
	}
	status := &RolloutStatus{
		Phase: decision.Phase, TargetGeneration: statusTarget,
		ActiveRelease: decision.ActiveRelease, ActiveWorkload: decision.ActiveWorkload,
		CandidateRelease:  decision.CandidateRelease,
		CandidateWorkload: decision.CandidateWorkload,
		StartedAt:         decision.StartedAt, FailedGeneration: decision.FailedGeneration,
		Rollback: decision.RollbackDrain,
	}
	if decision.DeleteCandidate {
		status.CandidateRelease = ""
		status.CandidateWorkload = ""
	}
	if decision.Phase == rolloutPhaseDraining {
		status.CutoverRevision = revision
		status.DrainingBackendIDs = append([]string(nil), drainingIDs...)
		status.DrainingWorkload = decision.DrainingWorkload
		if len(decision.DrainingBackendIDs) > 0 {
			status.DrainingBackendIDs = append([]string(nil), decision.DrainingBackendIDs...)
		}
	}
	return status
}

// routerRemoved acknowledges withdrawal after the loaded global document no longer
// contains a deployment and every attempt retained from its retired pool has finished.
func (r *Reconciler) routerRemoved(
	ctx context.Context, globalRevision, deploymentID string,
) (bool, error) {
	if globalRevision == "" || r.options.RouterStatusURL == "" {
		return false, nil
	}
	request, err := http.NewRequestWithContext(ctx, http.MethodGet,
		strings.TrimSuffix(r.options.RouterStatusURL, "/")+"/router-state", nil)
	if err != nil {
		return false, err
	}
	response, err := r.options.RouterHTTP.Do(request)
	if err != nil {
		return false, err
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return false, fmt.Errorf("router status returned %s", response.Status)
	}
	var observed routerState
	if err := json.NewDecoder(response.Body).Decode(&observed); err != nil {
		return false, err
	}
	if observed.Revision != globalRevision {
		return false, nil
	}
	for _, backend := range observed.Backends {
		if backend.DeploymentID == deploymentID && backend.InFlight > 0 {
			return false, nil
		}
	}
	return true, nil
}
