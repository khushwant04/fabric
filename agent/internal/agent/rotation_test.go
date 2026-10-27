package agent

import (
	"context"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/khushwant04/fabric/agent/internal/state"
)

func rotatingAgent(t *testing.T, url, dir string, maxAge time.Duration, now *time.Time) *Agent {
	t.Helper()
	instance, _ := newAgentAt(t, url, dir, "fab_enroll_token_secret")
	instance.config.CredentialMaxAge = maxAge
	instance.config.Now = func() time.Time { return *now }
	return instance
}

func TestARequestedRotationReplacesBothCredentials(t *testing.T) {
	stub := &controlPlaneStub{}
	server := stub.server(t)
	dir := t.TempDir()
	now := time.Date(2026, 10, 27, 12, 0, 0, 0, time.UTC)
	instance := rotatingAgent(t, server.URL, dir, 0, &now)
	if err := instance.Ensure(context.Background()); err != nil {
		t.Fatalf("ensure: %v", err)
	}

	stub.requestRotation.Store(true)
	if _, err := instance.ReconcileOnce(context.Background()); err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	if got := stub.rotations.Load(); got != 1 {
		t.Fatalf("rotations = %d, want 1", got)
	}

	saved, err := state.LoadCredentials(filepath.Join(dir, "credentials.json"))
	if err != nil {
		t.Fatalf("load: %v", err)
	}
	if saved.AgentCredential != "fab_agent_"+stampID+"_v2" {
		t.Errorf("persisted agent credential = %q", saved.AgentCredential)
	}
	if saved.CredentialsIssuedAt != now.Format(time.RFC3339) {
		t.Errorf("issued at = %q", saved.CredentialsIssuedAt)
	}
	telemetry, err := state.ReadTelemetryCredential(filepath.Join(dir, state.TelemetryCredentialFile))
	if err != nil || telemetry != "fab_telem_"+stampID+"_v2" {
		t.Errorf("collector credential = %q, %v", telemetry, err)
	}

	// The next pass presents the new credential and, the request served, rotates no more.
	if _, err := instance.ReconcileOnce(context.Background()); err != nil {
		t.Fatalf("second reconcile: %v", err)
	}
	if last := stub.presented[len(stub.presented)-1]; last != "Bearer fab_agent_"+stampID+"_v2" {
		t.Errorf("heartbeat after rotation presented %q", last)
	}
	if got := stub.rotations.Load(); got != 1 {
		t.Errorf("rotated again without a reason: %d", got)
	}
}

func TestCredentialsRotateOnceTheyReachTheirMaximumAge(t *testing.T) {
	stub := &controlPlaneStub{}
	server := stub.server(t)
	dir := t.TempDir()
	now := time.Date(2026, 10, 1, 0, 0, 0, 0, time.UTC)
	instance := rotatingAgent(t, server.URL, dir, 30*24*time.Hour, &now)
	if err := instance.Ensure(context.Background()); err != nil {
		t.Fatalf("ensure: %v", err)
	}

	now = now.Add(29 * 24 * time.Hour)
	if _, err := instance.ReconcileOnce(context.Background()); err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	if got := stub.rotations.Load(); got != 0 {
		t.Fatalf("rotated before the maximum age: %d", got)
	}

	now = now.Add(24 * time.Hour)
	if _, err := instance.ReconcileOnce(context.Background()); err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	if got := stub.rotations.Load(); got != 1 {
		t.Fatalf("rotations at the maximum age = %d, want 1", got)
	}
}

func TestACredentialThatCannotBePersistedIsNotUsed(t *testing.T) {
	stub := &controlPlaneStub{}
	server := stub.server(t)
	dir := t.TempDir()
	now := time.Date(2026, 10, 27, 12, 0, 0, 0, time.UTC)
	instance := rotatingAgent(t, server.URL, dir, 0, &now)
	credentialsDir := filepath.Join(dir, "identity")
	instance.config.CredentialsPath = filepath.Join(credentialsDir, "credentials.json")
	if err := instance.Ensure(context.Background()); err != nil {
		t.Fatalf("ensure: %v", err)
	}
	original := instance.credentials.AgentCredential

	// Replace the identity directory with a file, so the rotated pair cannot be saved.
	// Unlike a permission change this fails for root too.
	if err := os.RemoveAll(credentialsDir); err != nil {
		t.Fatalf("remove: %v", err)
	}
	if err := os.WriteFile(credentialsDir, []byte("not a directory"), 0o600); err != nil {
		t.Fatalf("block: %v", err)
	}

	stub.requestRotation.Store(true)
	if _, err := instance.ReconcileOnce(context.Background()); err != nil {
		t.Fatalf("a failed rotation must not fail the pass: %v", err)
	}
	if got := stub.rotations.Load(); got != 1 {
		t.Fatalf("rotations = %d, want 1", got)
	}
	if instance.credentials.AgentCredential != original || instance.client.Credential != original {
		t.Fatalf("switched to a credential that was never persisted")
	}
	telemetry, _ := state.ReadTelemetryCredential(filepath.Join(dir, state.TelemetryCredentialFile))
	if telemetry != "fab_telem_"+stampID+"_secret" {
		t.Errorf("collector was handed an unsaved credential: %q", telemetry)
	}
}

func TestAnUpgradedAgentStartsMeasuringAgeFromItsFirstStart(t *testing.T) {
	stub := &controlPlaneStub{}
	server := stub.server(t)
	dir := t.TempDir()
	legacy := &state.Credentials{
		StampID:             stampID,
		AccountID:           systemAcc,
		Mode:                "managed",
		AgentCredential:     "fab_agent_" + stampID + "_secret",
		TelemetryCredential: "fab_telem_" + stampID + "_secret",
	}
	if err := state.SaveCredentials(filepath.Join(dir, "credentials.json"), legacy); err != nil {
		t.Fatalf("save: %v", err)
	}
	now := time.Date(2026, 10, 27, 12, 0, 0, 0, time.UTC)
	instance := rotatingAgent(t, server.URL, dir, time.Hour, &now)
	if err := instance.Ensure(context.Background()); err != nil {
		t.Fatalf("ensure: %v", err)
	}
	if _, err := instance.ReconcileOnce(context.Background()); err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	if got := stub.rotations.Load(); got != 0 {
		t.Fatalf("a credential of unknown age was rotated at once: %d", got)
	}
	saved, _ := state.LoadCredentials(filepath.Join(dir, "credentials.json"))
	if saved.CredentialsIssuedAt != now.Format(time.RFC3339) {
		t.Errorf("issued at = %q, want first start", saved.CredentialsIssuedAt)
	}
}
