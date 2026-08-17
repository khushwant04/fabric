// Package agent reconciles control-plane desired state into local configuration.
//
// The loop is deliberately outbound-only: the control plane never connects to the
// cluster, so a stamp behind NAT or a restrictive firewall works with no inbound
// path. Each pass reads desired state, renders what the data plane needs, and
// reports what is observed.
//
// This agent does not create Kubernetes resources. That is the operator's job and
// the operator does not exist yet, so status is reported from what the agent
// itself applied. The distinction is recorded in the reported conditions rather
// than being implied.
package agent

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"time"

	"github.com/khushwant04/fabric/agent/internal/agentcontract"
	"github.com/khushwant04/fabric/agent/internal/controlplane"
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

	Capabilities   controlplane.Capabilities
	PollInterval   time.Duration
	RequestTimeout time.Duration
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
	// Status reports the control plane has not accepted yet, retried on later
	// passes because desired state will not mention them again.
	pendingStatus map[string]controlplane.DesiredDeployment
	// What the cluster reported, when an operator is present.
	observed map[string]agentcontract.ObservedCondition
	// The reason last accepted by the control plane per deployment, so a changed
	// verdict is sent and an unchanged one is not resent every pass.
	reportedReason map[string]string
}

// New builds an agent. It does not perform any network call.
func New(config Config, log *slog.Logger) *Agent {
	if config.PollInterval <= 0 {
		config.PollInterval = 15 * time.Second
	}
	if config.RequestTimeout <= 0 {
		config.RequestTimeout = 30 * time.Second
	}
	return &Agent{
		config:         config,
		client:         controlplane.New(config.ControlPlaneURL, config.RequestTimeout),
		log:            log,
		known:          map[string]state.Deployment{},
		pendingStatus:  map[string]controlplane.DesiredDeployment{},
		observed:       map[string]agentcontract.ObservedCondition{},
		reportedReason: map[string]string{},
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

	enrolled, err := a.client.Enroll(
		ctx, a.config.EnrollmentToken, a.config.StampName, a.config.Capabilities,
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

	capabilities := a.config.Capabilities
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
		a.reportedReason[id] = a.reasonFor(id)
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

	if observed, present := a.observed[assignment.DeploymentID]; present {
		// An operator reported on this deployment, so the cluster's verdict replaces
		// the agent's assertion.
		reason = observed.Reason
		message = observed.Message
		if !observed.Applied {
			phase = "pending"
		}
		if observed.ObservedGeneration > 0 {
			generation = int(observed.ObservedGeneration)
		}
	}

	report := controlplane.StatusReport{
		DeploymentID:       assignment.DeploymentID,
		ObservedGeneration: &generation,
		Phase:              phase,
		ReadyReplicas:      1,
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
		reason := a.reasonFor(deploymentID)
		if a.reportedReason[deploymentID] == reason {
			continue
		}
		if _, queued := a.pendingStatus[deploymentID]; queued {
			continue
		}
		a.pendingStatus[deploymentID] = controlplane.DesiredDeployment{
			DeploymentID: deploymentID,
			AccountID:    a.known[deploymentID].AccountID,
			ModelAlias:   a.known[deploymentID].ModelAlias,
			// The generation this agent has applied, which is what it can honestly
			// claim to have observed.
			DesiredGeneration: a.credentials.AckedGeneration,
		}
	}
}

// reasonFor returns the condition reason that would be reported now.
func (a *Agent) reasonFor(deploymentID string) string {
	if observed, present := a.observed[deploymentID]; present && observed.Reason != "" {
		return observed.Reason
	}
	return "AgentAppliedLocalConfiguration"
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
