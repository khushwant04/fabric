// Package agent reconciles control-plane desired state into local configuration.
//
// The loop is deliberately outbound-only: the control plane never connects to the
// cluster, so a stamp behind NAT or a restrictive firewall works with no inbound
// path. Each pass reads desired state, renders what the data plane needs, and
// reports what is observed.
//
// The agent itself does not create Kubernetes resources. That is the operator's job;
// status distinguishes what the agent wrote from what the operator actually observed.
package agent

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/khushwant04/fabric/agent/internal/agentcontract"
	"github.com/khushwant04/fabric/agent/internal/controlplane"
	"github.com/khushwant04/fabric/agent/internal/hardware"
	"github.com/khushwant04/fabric/agent/internal/state"
)

// Config is the agent's runtime configuration.
// Sink receives the assignments the agent has decided this stamp should serve.
//
// Two implementations exist. Without an operator the agent writes the data plane's
// file directly. With one, it declares intent as custom resources and the operator
// renders the file, which keeps central credentials and Kubernetes permissions in
// different processes.
type Sink interface {
	Apply(ctx context.Context, deployments []state.Deployment) error
}

// StatusSource is a sink that can also report what the cluster observed.
//
// When one is present the agent forwards the cluster's verdict instead of asserting
// its own, so the control plane learns what was actually applied rather than what the
// agent asked for. Without it the agent can only report what it wrote itself, and it
// says so in the condition reason.
type StatusSource interface {
	ObservedConditions(ctx context.Context) (map[string]agentcontract.ObservedCondition, error)
}

// CapacitySource measures what this stamp can offer.
//
// The agent used to report a capability document built once at startup from flags, which
// meant `gpus: []` and `requested_gpus: 0` on every heartbeat forever. The control plane
// decides placement against those numbers (ADR 0013), so they have to be measured rather
// than declared. Nil leaves the configured values in place, which is right for a stamp
// with no Kubernetes access to read.
type CapacitySource interface {
	Measure(ctx context.Context) (hardware.Capacity, error)
}

// CapacityFunc adapts a function to CapacitySource.
type CapacityFunc func(ctx context.Context) (hardware.Capacity, error)

// Measure calls the wrapped function.
func (f CapacityFunc) Measure(ctx context.Context) (hardware.Capacity, error) {
	return f(ctx)
}

type Config struct {
	ControlPlaneURL string
	EnrollmentToken string
	StampName       string
	CredentialsPath string
	DeploymentsPath string
	// TelemetryCredentialPath receives the write-only telemetry credential for
	// the collector. Empty disables the hand-off, for a stamp running no
	// collector.
	TelemetryCredentialPath string
	// Sink overrides where assignments are published. Nil writes the data plane's
	// file, which is the behaviour for a stamp with no operator.
	Sink Sink
	// SinkFactory builds a sink once the stamp id is known. Enrollment assigns that
	// id, so a publisher that labels resources with it cannot be built before then.
	SinkFactory func(stampID string) Sink

	// UpstreamURL is the model host this stamp serves from. The agent records it
	// in the data plane's configuration; it does not start the host.
	UpstreamURL string

	// Capabilities is what this stamp reports about itself. The GPU fields are a
	// fallback: when Capacity can read the cluster, the measurement replaces them.
	Capabilities controlplane.Capabilities
	// Capacity measures the cluster's GPUs. Nil reports Capabilities unchanged.
	Capacity CapacitySource
	// CapacityInterval is how often the cluster is re-measured. Node hardware does not
	// move, but pod claims do, so this is far shorter than "once" and far longer than
	// every pass.
	CapacityInterval time.Duration
	PollInterval     time.Duration
	RequestTimeout   time.Duration
}

// Agent holds one stamp's reconciliation state.
type Agent struct {
	config      Config
	client      *controlplane.Client
	credentials *state.Credentials
	log         *slog.Logger

	// known tracks assignments seen so far, because desired state is delivered
	// incrementally by generation and a later pass returns only what changed.
	known map[string]state.Deployment
	// verification is how the data plane must check tokens, as the control plane last
	// reported it. Held on the agent rather than on an assignment because it describes the
	// stamp: desired state returns only deployments newer than the acknowledged
	// generation, so a steady-state pass carries no assignments at all while still
	// carrying this. An empty value means the control plane has not told us, which leaves
	// the data plane on its locally configured values.
	verification state.Verification
	// Status reports the control plane has not accepted yet, retried on later
	// passes because desired state will not mention them again.
	pendingStatus map[string]controlplane.DesiredDeployment
	// What the cluster reported, when an operator is present.
	observed map[string]agentcontract.ObservedCondition
	// The cluster observation last accepted by the control plane per deployment, so a
	// changed phase or replica count is sent and an unchanged one is not resent.
	reportedObservation map[string]string

	// The last successful capacity measurement and when it was taken. Kept rather than
	// re-read every pass, and kept rather than discarded on a failed read: last known
	// hardware is a better answer than none, and reporting zero GPUs would make a live
	// stamp look unplaceable because one API call timed out.
	measured   hardware.Capacity
	measuredAt time.Time
}

// New builds an agent. It does not perform any network call.
func New(config Config, log *slog.Logger) *Agent {
	if config.PollInterval <= 0 {
		config.PollInterval = 15 * time.Second
	}
	if config.RequestTimeout <= 0 {
		config.RequestTimeout = 30 * time.Second
	}
	if config.CapacityInterval <= 0 {
		config.CapacityInterval = time.Minute
	}
	return &Agent{
		config:              config,
		client:              controlplane.New(config.ControlPlaneURL, config.RequestTimeout),
		log:                 log,
		known:               map[string]state.Deployment{},
		pendingStatus:       map[string]controlplane.DesiredDeployment{},
		observed:            map[string]agentcontract.ObservedCondition{},
		reportedObservation: map[string]string{},
	}
}

// StampID returns the enrolled stamp id, empty before enrollment.
func (a *Agent) StampID() string {
	if a.credentials == nil {
		return ""
	}
	return a.credentials.StampID
}

// Ensure loads persisted credentials or enrolls once.
//
// Enrollment tokens are single use, so an agent that already holds credentials
// must never enroll again: doing so would consume a second token and register a
// duplicate stamp.
func (a *Agent) Ensure(ctx context.Context) error {
	existing, err := state.LoadCredentials(a.config.CredentialsPath)
	if err != nil {
		return err
	}
	if existing != nil {
		a.credentials = existing
		a.client.Credential = existing.AgentCredential
		// The collector's copy may live on an ephemeral volume, so it is rewritten
		// on every start rather than only when the credential was first issued.
		a.handOffTelemetryCredential()
		a.buildSink()
		// A restart also loses unreported status, and desired state will not
		// mention already-acknowledged assignments again. What is on disk is what
		// this agent has applied, so it is reported at the acknowledged generation.
		existingDeployments, readErr := state.ReadDeployments(a.config.DeploymentsPath)
		switch {
		case readErr == nil:
			for _, deployment := range existingDeployments.Deployments {
				a.known[deployment.DeploymentID] = deployment
				a.pendingStatus[deployment.DeploymentID] = controlplane.DesiredDeployment{
					DeploymentID:      deployment.DeploymentID,
					AccountID:         deployment.AccountID,
					ModelAlias:        deployment.ModelAlias,
					DesiredGeneration: existing.AckedGeneration,
				}
			}
		case a.credentials.AckedGeneration > 0:
			// The rendered configuration is gone while the acknowledged generation
			// says assignments were applied. Desired state is delivered
			// incrementally, so asking for changes after that generation would
			// return nothing and this stamp would serve nothing for as long as it
			// ran. Credentials are durable and the rendered file usually is not, so
			// this is the normal consequence of a restart, not a rare corruption.
			//
			// Forgetting the acknowledgement asks for the full set again. It is safe
			// because rendering is idempotent: the same assignments produce the same
			// file.
			a.log.Warn("rendered configuration is missing, rebuilding from full desired state",
				"path", a.config.DeploymentsPath,
				"acked_generation", a.credentials.AckedGeneration,
				"error", readErr)
			a.credentials.AckedGeneration = 0
		}
		a.log.Info("using persisted stamp identity",
			"stamp_id", existing.StampID, "acked_generation", existing.AckedGeneration)
		return nil
	}

	if a.config.EnrollmentToken == "" {
		return errors.New("no credentials on disk and no enrollment token supplied")
	}

	// Measured before enrolling, so the stamp is placeable from its first heartbeat
	// rather than from whenever the first measurement happens to land.
	a.refreshCapacity(ctx)

	enrolled, err := a.client.Enroll(
		ctx, a.config.EnrollmentToken, a.config.StampName, a.reportedCapabilities(),
	)
	if err != nil {
		return fmt.Errorf("enroll: %w", err)
	}

	a.credentials = &state.Credentials{
		StampID:             enrolled.Stamp.ID,
		AccountID:           enrolled.Stamp.AccountID,
		Mode:                enrolled.Stamp.Mode,
		AgentCredential:     enrolled.AgentCredential,
		TelemetryCredential: enrolled.TelemetryCredential,
	}
	if err := state.SaveCredentials(a.config.CredentialsPath, a.credentials); err != nil {
		// The credentials exist server-side but could not be persisted. Surfacing
		// this is essential: the token is now spent and a silent failure would
		// strand the stamp.
		return fmt.Errorf("persist credentials for stamp %s: %w", enrolled.Stamp.ID, err)
	}
	a.handOffTelemetryCredential()
	a.buildSink()

	a.client.Credential = enrolled.AgentCredential
	a.log.Info("enrolled",
		"stamp_id", enrolled.Stamp.ID, "mode", enrolled.Stamp.Mode,
		"stamp_account", enrolled.Stamp.AccountID)
	return nil
}

// refreshCapacity re-measures the cluster's GPUs when the last measurement is stale.
//
// A failure is a warning, not an error. The control plane decides placement against this
// report, and the honest degradation is to keep reporting the last hardware seen, or the
// configured fallback if none has ever been seen, rather than to stop reconciling because
// a node list timed out.
func (a *Agent) refreshCapacity(ctx context.Context) {
	if a.config.Capacity == nil {
		return
	}
	if a.measured.Measured && time.Since(a.measuredAt) < a.config.CapacityInterval {
		return
	}

	capacity, err := a.config.Capacity.Measure(ctx)
	if err != nil {
		// Logged at warn even when part of the read succeeded, because an unmeasured
		// stamp is one whose placements are decided on stale or declared numbers.
		a.log.Warn("could not measure this stamp's capacity", "error", err)
	}
	if !capacity.Measured {
		return
	}
	if !capacity.PodsMeasured {
		// Without pod claims, GPUs held by workloads Fabric did not place are invisible and
		// the stamp looks emptier than it is. Logged here and also reported, because the
		// control plane cannot otherwise tell an unmeasured stamp from an idle one and it
		// decides placements on the difference.
		a.log.Warn("reporting capacity without pod claims; foreign gpu use is not visible",
			"allocatable_gpus", capacity.AllocatableGPUs, "error", capacity.PodClaimsError)
	}
	a.measured = capacity
	a.measuredAt = time.Now()
	a.log.Info("measured stamp capacity",
		"allocatable_gpus", capacity.AllocatableGPUs,
		"requested_gpus", capacity.RequestedGPUs,
		"fabric_requested_gpus", capacity.FabricRequestedGPUs,
		"max_gpus_per_node", capacity.MaxGPUsPerNode,
		"gpu_classes", len(capacity.GPUs))
}

// reportedCapabilities is the capability document to send.
//
// Configured values supply everything the cluster cannot answer — orchestrator, region,
// versions — and a measurement replaces only the GPU facts, so a stamp that cannot read
// its cluster still reports what it was told.
func (a *Agent) reportedCapabilities() controlplane.Capabilities {
	capabilities := a.config.Capabilities
	if capabilities.FabricGPUClaims == nil {
		capabilities.FabricGPUClaims = []controlplane.GPUClaim{}
	}
	if capabilities.AvailableGPUSlots == nil {
		capabilities.AvailableGPUSlots = []int{}
	}
	if !a.measured.Measured {
		if capabilities.GPUs == nil {
			capabilities.GPUs = []controlplane.GPU{}
		}
		return capabilities
	}

	gpus := make([]controlplane.GPU, 0, len(a.measured.GPUs))
	for _, group := range a.measured.GPUs {
		gpus = append(gpus, controlplane.GPU{
			Product:           group.Product,
			Count:             group.Count,
			MemoryBytes:       group.MemoryBytes,
			ComputeCapability: group.ComputeCapability,
		})
	}
	capabilities.GPUs = gpus
	capabilities.AllocatableGPUs = a.measured.AllocatableGPUs
	capabilities.MaxGPUsPerNode = a.measured.MaxGPUsPerNode
	capabilities.MaxFreeGPUsPerNode = a.measured.MaxFreeGPUsPerNode
	capabilities.AvailableGPUSlots = append(
		[]int{}, a.measured.AvailableGPUSlots...,
	)
	capabilities.RequestedGPUs = a.measured.RequestedGPUs
	capabilities.FabricRequestedGPUs = a.measured.FabricRequestedGPUs
	capabilities.GPUClaimsMeasured = a.measured.PodsMeasured

	claims := a.reportedFabricClaims()
	capabilities.FabricGPUClaims = claims
	return capabilities
}

// reportedFabricClaims puts live control-plane assignments before every other measured
// Fabric pod, then applies the bounded wire cap.
//
// The hardware layer sees terminating and out-of-band pods too. If it truncated before this
// ordering, one of those could permanently displace a supported live deployment id, making a
// multi-GPU observation wait impossible to clear. The agent's known map is the authoritative
// set it is reconciling; after a deletion/replacement race it catches up on the same desired-
// state pass, so a newly live id is prioritized on the next heartbeat.
func (a *Agent) reportedFabricClaims() []controlplane.GPUClaim {
	byID := make(map[string]int, len(a.measured.FabricClaims))
	for _, claim := range a.measured.FabricClaims {
		byID[claim.DeploymentID] += claim.GPUs
	}

	live := make([]string, 0, len(a.known))
	for deploymentID := range a.known {
		if byID[deploymentID] > 0 {
			live = append(live, deploymentID)
		}
	}
	sort.Strings(live)

	claims := make([]controlplane.GPUClaim, 0, min(len(byID), hardware.MaxReportedClaims))
	included := make(map[string]bool, len(live))
	for _, deploymentID := range live {
		claims = append(claims, controlplane.GPUClaim{
			DeploymentID: deploymentID,
			GPUs:         byID[deploymentID],
		})
		included[deploymentID] = true
		if len(claims) == hardware.MaxReportedClaims {
			return claims
		}
	}

	// Measurement already orders the remainder by GPUs descending then id, preserving the
	// most consequential unsupported claims when room remains.
	for _, claim := range a.measured.FabricClaims {
		if included[claim.DeploymentID] {
			continue
		}
		claims = append(claims, controlplane.GPUClaim{
			DeploymentID: claim.DeploymentID,
			GPUs:         claim.GPUs,
		})
		if len(claims) == hardware.MaxReportedClaims {
			break
		}
	}
	return claims
}

// handOffTelemetryCredential writes the collector's credential if one is wanted.
//
// This runs on every start, not only after enrollment. Credentials persist on a
// volume that survives restarts, while the collector's copy usually lives on an
// ephemeral one, so a restarted agent that only wrote at enrollment would leave the
// collector with no credential and usage would stop being reported.
func (a *Agent) handOffTelemetryCredential() {
	if a.config.TelemetryCredentialPath == "" || a.credentials == nil {
		return
	}
	if err := state.WriteTelemetryCredential(
		a.config.TelemetryCredentialPath, a.credentials.TelemetryCredential,
	); err != nil {
		// Not fatal: usage export is degraded, but inference and status reporting
		// still work, and refusing to run would be the worse outcome.
		a.log.Error("could not hand the telemetry credential to the collector",
			"path", a.config.TelemetryCredentialPath, "error", err)
	}
}

// ReconcileOnce performs one pass and returns the assignments now configured.
func (a *Agent) ReconcileOnce(ctx context.Context) ([]state.Deployment, error) {
	if a.credentials == nil {
		return nil, errors.New("not enrolled")
	}

	a.refreshCapacity(ctx)
	capabilities := a.reportedCapabilities()
	if err := a.client.Heartbeat(ctx, a.credentials.StampID, &capabilities); err != nil {
		return nil, fmt.Errorf("heartbeat: %w", err)
	}

	desired, err := a.client.DesiredState(
		ctx, a.credentials.StampID, a.credentials.AckedGeneration,
	)
	if err != nil {
		return nil, fmt.Errorf("desired state: %w", err)
	}

	changed := false
	// Checked before the assignments, and on every pass rather than only when an
	// assignment arrives: a steady-state poll returns no deployments at all, and a stamp
	// whose issuer is wrong has to converge without waiting for an unrelated change.
	if updated := verificationFrom(desired.Verification); updated != a.verification {
		if updated != (state.Verification{}) {
			a.log.Info("token verification updated from the control plane",
				"issuer", updated.JWTIssuer, "jwks_url", updated.JWKSURL)
		}
		a.verification = updated
		changed = true
	}

	for _, assignment := range desired.Deployments {
		if assignment.Deleted {
			if _, present := a.known[assignment.DeploymentID]; present {
				delete(a.known, assignment.DeploymentID)
				changed = true
				a.log.Info("withdrew deployment", "deployment_id", assignment.DeploymentID)
			}
			continue
		}
		entry := state.Deployment{
			DeploymentID: assignment.DeploymentID,
			// The owning customer account, which for a managed stamp is not the
			// account that owns the stamp.
			AccountID:   assignment.AccountID,
			ModelAlias:  assignment.ModelAlias,
			UpstreamURL: a.config.UpstreamURL,
		}
		if release := releaseFromSpec(assignment.Spec); release != "" {
			entry.UpstreamModel = release
		}
		entry.KernelMode = kernelModeFromSpec(assignment.Spec)
		entry.Replicas = replicasFromSpec(assignment.Spec)
		entry.Strategy = strategyFromSpec(assignment.Spec)
		entry.Capabilities = modelCapabilitiesFromSpec(assignment.Spec)
		entry.GPUCount = gpuCountFromSpec(assignment.Spec)
		entry.MaxModelLen = maxModelLenFromSpec(assignment.Spec)
		entry.MaxNumSeqs = maxNumSeqsFromSpec(assignment.Spec)
		entry.GPUMemoryUtilization = gpuMemoryUtilizationFromSpec(assignment.Spec)
		entry.Execution = executionFromSpec(assignment.Spec)
		if previous, present := a.known[assignment.DeploymentID]; !present || previous != entry {
			changed = true
			a.log.Info("configured deployment",
				"deployment_id", assignment.DeploymentID,
				"account_id", assignment.AccountID,
				"model_alias", assignment.ModelAlias,
				"generation", assignment.DesiredGeneration)
		}
		a.known[assignment.DeploymentID] = entry
	}

	configured := a.configured()
	if changed {
		if err := a.publish(ctx, configured); err != nil {
			return nil, err
		}
	}

	// Acknowledge only after the configuration is on disk, so a crash mid-pass
	// replays the assignment instead of losing it.
	if desired.MaxGeneration > a.credentials.AckedGeneration {
		a.credentials.AckedGeneration = desired.MaxGeneration
		if err := state.SaveCredentials(a.config.CredentialsPath, a.credentials); err != nil {
			return nil, fmt.Errorf("persist acknowledged generation: %w", err)
		}
	}

	// Desired state is requested after the acknowledged generation, so a later
	// pass returns nothing new. A status write that failed here would never be
	// attempted again, leaving a serving deployment with no reported status at all,
	// so unaccepted reports are held and retried.
	// Refreshed once per pass rather than per report, so a stamp with many
	// assignments makes one call instead of one per deployment.
	a.refreshObserved(ctx)

	for _, assignment := range desired.Deployments {
		a.pendingStatus[assignment.DeploymentID] = assignment
	}
	a.queueChangedVerdicts()

	for id, assignment := range a.pendingStatus {
		if err := a.reportStatus(ctx, assignment); err != nil {
			// A status write failing does not invalidate the configuration that
			// was already applied, so the pass is not aborted.
			a.log.Warn("status report failed, will retry",
				"deployment_id", assignment.DeploymentID, "error", err)
			continue
		}
		a.reportedObservation[id] = a.observationFingerprint(id)
		delete(a.pendingStatus, id)
	}
	return configured, nil
}

func (a *Agent) reportStatus(ctx context.Context, assignment controlplane.DesiredDeployment) error {
	generation := assignment.DesiredGeneration
	phase := "ready"
	if assignment.Deleted {
		phase = "terminating"
	}
	reason := "AgentAppliedLocalConfiguration"
	// Truthful by default: with no operator the agent can only report what it wrote
	// itself, which is not an observed rollout.
	message := "Local data-plane configuration written by the agent"

	desiredReplicas := replicasFromSpec(assignment.Spec)
	if desiredReplicas < 1 {
		desiredReplicas = 1
	}
	readyReplicas := desiredReplicas
	unavailableReplicas := 0

	observed, observationPresent := a.observed[assignment.DeploymentID]
	_, operatorBacked := a.config.Sink.(StatusSource)
	if operatorBacked && !observationPresent && !assignment.Deleted {
		// Declaring a CR is not evidence that any pod is ready. Fail closed until the
		// operator has written status rather than briefly claiming the whole requested
		// fleet is serving during every new placement.
		phase = "pending"
		readyReplicas = 0
		unavailableReplicas = desiredReplicas
		reason = "AwaitingOperatorObservation"
		message = "Intent declared; waiting for the operator to observe model-host readiness"
	}

	if observationPresent {
		// An operator reported on this deployment, so the cluster's verdict replaces
		// the agent's assertion.
		reason = observed.Reason
		message = observed.Message
		if observed.Phase != "" {
			phase = observed.Phase
		} else if !observed.Applied {
			phase = "pending"
		}
		if observed.ObservedGeneration > 0 {
			generation = int(observed.ObservedGeneration)
		}
		if observed.ReadyReplicas != nil {
			readyReplicas = *observed.ReadyReplicas
		}
		if observed.UnavailableReplicas != nil {
			unavailableReplicas = *observed.UnavailableReplicas
		}
	}

	report := controlplane.StatusReport{
		DeploymentID:        assignment.DeploymentID,
		ObservedGeneration:  &generation,
		Phase:               phase,
		ReadyReplicas:       readyReplicas,
		UnavailableReplicas: unavailableReplicas,
		Conditions: []map[string]any{
			{
				"type":    "Configured",
				"status":  "True",
				"reason":  reason,
				"message": message,
			},
		},
	}
	if assignment.Deleted {
		report.ReadyReplicas = 0
	}
	return a.client.ReportStatus(ctx, a.credentials.StampID, report)
}

// buildSink attaches the publisher once the stamp id is known.
func (a *Agent) buildSink() {
	if a.config.Sink != nil || a.config.SinkFactory == nil || a.credentials == nil {
		return
	}
	a.config.Sink = a.config.SinkFactory(a.credentials.StampID)
}

// queueChangedVerdicts re-reports a deployment whose observed status has changed.
//
// An operator reconciles after the agent declares intent, so the first pass reports
// the agent's own view and the operator's verdict arrives later. Without this the
// control plane would keep the first answer forever and never learn what the cluster
// actually did.
func (a *Agent) queueChangedVerdicts() {
	for deploymentID := range a.known {
		fingerprint := a.observationFingerprint(deploymentID)
		if a.reportedObservation[deploymentID] == fingerprint {
			continue
		}
		if _, queued := a.pendingStatus[deploymentID]; queued {
			continue
		}
		a.pendingStatus[deploymentID] = controlplane.DesiredDeployment{
			DeploymentID: deploymentID,
			AccountID:    a.known[deploymentID].AccountID,
			ModelAlias:   a.known[deploymentID].ModelAlias,
			// Preserve the spec so the fallback replica count remains truthful when an
			// older operator does not publish replica status yet.
			Spec: map[string]any{"replicas": a.known[deploymentID].Replicas},
			// The generation this agent has applied, which is what it can honestly
			// claim to have observed.
			DesiredGeneration: a.credentials.AckedGeneration,
		}
	}
}

// observationFingerprint is the status detail whose change requires another report.
func (a *Agent) observationFingerprint(deploymentID string) string {
	observed, present := a.observed[deploymentID]
	if !present {
		if _, operatorBacked := a.config.Sink.(StatusSource); operatorBacked {
			return "AwaitingOperatorObservation"
		}
		return "AgentAppliedLocalConfiguration"
	}
	ready, unavailable := -1, -1
	if observed.ReadyReplicas != nil {
		ready = *observed.ReadyReplicas
	}
	if observed.UnavailableReplicas != nil {
		unavailable = *observed.UnavailableReplicas
	}
	return fmt.Sprintf(
		"%s|%s|%t|%d|%d|%d",
		observed.Phase, observed.Reason, observed.Applied,
		observed.ObservedGeneration, ready, unavailable,
	)
}

// refreshObserved reads what the cluster reported, when a sink can tell us.
func (a *Agent) refreshObserved(ctx context.Context) {
	source, ok := a.config.Sink.(StatusSource)
	if !ok {
		return
	}
	observed, err := source.ObservedConditions(ctx)
	if err != nil {
		// Reporting the agent's own view is better than reporting nothing, so a
		// failure here degrades the detail rather than the delivery.
		a.log.Warn("could not read observed status from the cluster", "error", err)
		return
	}
	a.observed = observed
}

// publish hands the decided assignments to whichever sink is configured.
func (a *Agent) publish(ctx context.Context, configured []state.Deployment) error {
	if a.config.Sink != nil {
		if err := a.config.Sink.Apply(ctx, configured); err != nil {
			return fmt.Errorf("declare deployments: %w", err)
		}
		return nil
	}
	if err := state.WriteDeployments(a.config.DeploymentsPath, configured); err != nil {
		return fmt.Errorf("write deployments: %w", err)
	}
	return nil
}

func (a *Agent) configured() []state.Deployment {
	entries := make([]state.Deployment, 0, len(a.known))
	for _, entry := range a.known {
		// Stamped on here rather than stored per assignment, so a verification change does
		// not require rewriting every remembered entry and cannot leave two of them
		// disagreeing about one stamp's issuer.
		entry.Verification = a.verification
		entries = append(entries, entry)
	}
	return entries
}

// kernelModeFromSpec extracts which decode kernel the deployment asks for.
//
// The control plane has accepted this field since the beginning and nothing acted on it,
// so a deployment could declare a kernel and be served by another. An unrecognised value
// is treated as unset rather than rejected here: the control plane validates the vocabulary
// and an agent that refused a value a newer control plane understands would stop
// reconciling entirely.
func kernelModeFromSpec(spec map[string]any) string {
	runtime, ok := spec["runtime"].(map[string]any)
	if !ok {
		return ""
	}
	mode, _ := runtime["kernel_mode"].(string)
	switch mode {
	case "fabric", "standard", "auto":
		return mode
	default:
		return ""
	}
}

// replicasFromSpec extracts how many model-host replicas the deployment asks for.
//
// The control plane's DeploymentSpec has carried replicas (1-32) since the beginning and
// the operator ignored it, hardcoding one, so a deployment could ask for a fleet and get
// a single host (ADR 0010). An absent, non-numeric, or below-one value is reported as
// zero, which the operator reads as the single replica the deployment has always had:
// the agent does not reject a value a newer control plane might send, for the same reason
// kernelModeFromSpec does not.
func replicasFromSpec(spec map[string]any) int {
	// JSON numbers decode into float64 through an any-typed map, so both shapes are
	// accepted rather than assuming one decoder.
	switch value := spec["replicas"].(type) {
	case float64:
		if value >= 1 {
			return int(value)
		}
	case int:
		if value >= 1 {
			return value
		}
	}
	return 0
}

// strategyFromSpec extracts how the data plane should balance across this deployment's
// backends (M2, ADR 0011).
//
// It lives under the runtime sub-spec beside kernel_mode, because both are properties of
// how the deployment is served. An unrecognised value is treated as unset rather than
// rejected, matching kernelModeFromSpec: the control plane validates the vocabulary, and
// an agent that refused a value a newer control plane understands would stop reconciling
// entirely. Unset carries through as empty, which the data plane reads as its own
// default of least-in-flight.
func strategyFromSpec(spec map[string]any) string {
	runtime, ok := spec["runtime"].(map[string]any)
	if !ok {
		return ""
	}
	strategy, _ := runtime["strategy"].(string)
	switch strategy {
	case "least_in_flight", "round_robin", "session_affinity", "weighted":
		return strategy
	default:
		return ""
	}
}

// modelCapabilitiesFromSpec extracts the bounded metadata used by model=auto.
// Missing metadata describes a legacy text model, preserving compatibility while
// keeping structurally different vision and audio inputs opt-in.
func modelCapabilitiesFromSpec(spec map[string]any) state.ModelCapabilities {
	result := state.ModelCapabilities{AutoEnabled: true, Chat: true, Completion: true}
	capabilities, ok := spec["capabilities"].(map[string]any)
	if !ok {
		return result
	}
	setBool := func(name string, target *bool) {
		if value, present := capabilities[name].(bool); present {
			*target = value
		}
	}
	setBool("auto_enabled", &result.AutoEnabled)
	setBool("chat", &result.Chat)
	setBool("completion", &result.Completion)
	setBool("vision", &result.Vision)
	setBool("transcription", &result.Transcription)
	setBool("translation", &result.Translation)
	setBool("code", &result.Code)
	setBool("reasoning", &result.Reasoning)
	switch value := capabilities["priority"].(type) {
	case float64:
		if value >= -100 && value <= 100 {
			result.Priority = int(value)
		}
	case int:
		if value >= -100 && value <= 100 {
			result.Priority = value
		}
	}
	return result
}

// gpuCountFromSpec extracts how many devices one replica of this deployment needs.
//
// The control plane has validated resources.gpu_count (1-8) since the beginning and nothing
// read it: the pod's GPU limit came from a per-stamp Helm value instead, so a deployment
// could ask for four devices and be given one. Placement is now admitted against
// replicas x this number (ADR 0013), which only means something if the same number reaches
// the pod. An absent, non-numeric, or below-one value is reported as zero, which the
// operator reads as the stamp's configured count, so a deployment that predates the field
// is unchanged.
func gpuCountFromSpec(spec map[string]any) int {
	resources, ok := spec["resources"].(map[string]any)
	if !ok {
		return 0
	}
	// JSON numbers decode into float64 through an any-typed map, so both shapes are
	// accepted rather than assuming one decoder.
	switch value := resources["gpu_count"].(type) {
	case float64:
		if value >= 1 {
			return int(value)
		}
	case int:
		if value >= 1 {
			return value
		}
	}
	return 0
}

// runtimeInt extracts a positive integer from the runtime sub-spec.
//
// JSON numbers decode into float64 through an any-typed map, so both shapes are accepted
// rather than assuming one decoder. An absent, non-numeric, or below-one value is reported
// as zero, which the operator reads as the stamp's configured value.
func runtimeInt(spec map[string]any, field string) int {
	runtime, ok := spec["runtime"].(map[string]any)
	if !ok {
		return 0
	}
	switch value := runtime[field].(type) {
	case float64:
		if value >= 1 {
			return int(value)
		}
	case int:
		if value >= 1 {
			return value
		}
	}
	return 0
}

// maxModelLenFromSpec extracts the context bound this deployment asked for.
//
// Before this existed the bound came from a per-stamp Helm value, so one number had to
// suit every model on the stamp: too large and a short-context model refuses to start,
// too small and a long-context model is capped below what it can do.
func maxModelLenFromSpec(spec map[string]any) int {
	return runtimeInt(spec, "max_model_len")
}

// maxNumSeqsFromSpec extracts the concurrent-sequence bound this deployment asked for.
func maxNumSeqsFromSpec(spec map[string]any) int {
	return runtimeInt(spec, "max_num_seqs")
}

// gpuMemoryUtilizationFromSpec extracts the memory fraction as a string.
//
// Formatted with the shortest representation that round-trips, so 0.85 reaches the server
// as "0.85" rather than as a long decimal expansion of the nearest float64. Values outside
// (0,1) are treated as unset: the fraction is of total device memory, so one means asking
// for memory the runtime itself needs.
func gpuMemoryUtilizationFromSpec(spec map[string]any) string {
	runtime, ok := spec["runtime"].(map[string]any)
	if !ok {
		return ""
	}
	var fraction float64
	switch value := runtime["gpu_memory_utilization"].(type) {
	case float64:
		fraction = value
	case int:
		fraction = float64(value)
	default:
		return ""
	}
	if fraction <= 0 || fraction >= 1 {
		return ""
	}
	return strconv.FormatFloat(fraction, 'g', -1, 64)
}

// executionFromSpec extracts whether the host should capture CUDA graphs.
//
// A mode rather than a boolean so that a deployment expressing no opinion stays
// distinguishable from one explicitly asking not to capture. An unrecognised value is
// treated as unset rather than rejected, matching the other extractors.
func executionFromSpec(spec map[string]any) string {
	runtime, ok := spec["runtime"].(map[string]any)
	if !ok {
		return ""
	}
	execution, _ := runtime["execution"].(string)
	switch execution {
	case "eager", "cuda_graph":
		return execution
	default:
		return ""
	}
}

// verificationFrom reads the control plane's reported verification contract.
//
// A partial answer is discarded rather than half-applied: an issuer with no key source, or
// keys with no issuer, cannot verify anything, and adopting half of it would replace a
// working local configuration with one that rejects everything. Nil or incomplete
// therefore means "the control plane has not told us", which leaves the data plane on the
// values it already has.
func verificationFrom(reported *controlplane.VerificationConfig) state.Verification {
	if reported == nil {
		return state.Verification{}
	}
	issuer := strings.TrimSpace(reported.JWTIssuer)
	jwks := strings.TrimSpace(reported.JWKSURL)
	if issuer == "" || jwks == "" {
		return state.Verification{}
	}
	return state.Verification{JWTIssuer: issuer, JWKSURL: jwks}
}

// releaseFromSpec extracts the runtime release the host should serve.
func releaseFromSpec(spec map[string]any) string {
	runtime, ok := spec["runtime"].(map[string]any)
	if !ok {
		return ""
	}
	release, _ := runtime["release"].(string)
	return release
}

// Run reconciles until the context is cancelled.
//
// A permanent control-plane rejection stops the loop: a revoked credential or a
// spent enrollment token never recovers, and retrying would only add load.
func (a *Agent) Run(ctx context.Context) error {
	if err := a.Ensure(ctx); err != nil {
		return err
	}

	ticker := time.NewTicker(a.config.PollInterval)
	defer ticker.Stop()

	for {
		if _, err := a.ReconcileOnce(ctx); err != nil {
			var apiErr *controlplane.APIError
			if errors.As(err, &apiErr) && !apiErr.Retryable() {
				return fmt.Errorf("stopping: %w", err)
			}
			a.log.Warn("reconcile failed, will retry", "error", err)
		}

		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-ticker.C:
		}
	}
}
