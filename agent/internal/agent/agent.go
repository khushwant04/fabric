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

	"github.com/khushwant04/fabric/agent/internal/controlplane"
	"github.com/khushwant04/fabric/agent/internal/state"
)

// Config is the agent's runtime configuration.
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
		config:        config,
		client:        controlplane.New(config.ControlPlaneURL, config.RequestTimeout),
		log:           log,
		known:         map[string]state.Deployment{},
		pendingStatus: map[string]controlplane.DesiredDeployment{},
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
		if err := state.WriteDeployments(a.config.DeploymentsPath, configured); err != nil {
			return nil, fmt.Errorf("write deployments: %w", err)
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
	for _, assignment := range desired.Deployments {
		a.pendingStatus[assignment.DeploymentID] = assignment
	}

	for id, assignment := range a.pendingStatus {
		if err := a.reportStatus(ctx, assignment); err != nil {
			// A status write failing does not invalidate the configuration that
			// was already applied, so the pass is not aborted.
			a.log.Warn("status report failed, will retry",
				"deployment_id", assignment.DeploymentID, "error", err)
			continue
		}
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
	report := controlplane.StatusReport{
		DeploymentID:       assignment.DeploymentID,
		ObservedGeneration: &generation,
		Phase:              phase,
		ReadyReplicas:      1,
		Conditions: []map[string]any{
			{
				"type":   "Configured",
				"status": "True",
				"reason": "AgentAppliedLocalConfiguration",
				// No operator exists, so this reports what the agent configured,
				// not an observed Kubernetes rollout.
				"message": "Local data-plane configuration written by the agent",
			},
		},
	}
	if assignment.Deleted {
		report.ReadyReplicas = 0
	}
	return a.client.ReportStatus(ctx, a.credentials.StampID, report)
}

func (a *Agent) configured() []state.Deployment {
	entries := make([]state.Deployment, 0, len(a.known))
	for _, entry := range a.known {
		entries = append(entries, entry)
	}
	return entries
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
