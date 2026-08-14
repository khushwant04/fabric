package agent

import (
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"sync/atomic"
	"testing"

	"github.com/khushwant04/fabric/agent/internal/controlplane"
	"github.com/khushwant04/fabric/agent/internal/state"
)

const (
	stampID   = "5f0e6d2a-0000-4000-8000-000000000001"
	systemAcc = "5f0e6d2a-0000-4000-8000-0000000000ff"
	customerA = "aaaaaaaa-0000-4000-8000-00000000000a"
	customerB = "bbbbbbbb-0000-4000-8000-00000000000b"
	deployA   = "dddddddd-0000-4000-8000-00000000000a"
	deployB   = "dddddddd-0000-4000-8000-00000000000b"
)

func discardLogger() *slog.Logger {
	return slog.New(slog.NewTextHandler(io.Discard, nil))
}

// controlPlaneStub records what the agent sent and serves canned desired state.
type controlPlaneStub struct {
	enrollments  atomic.Int32
	heartbeats   atomic.Int32
	statuses     []controlplane.StatusReport
	desired      []controlplane.DesiredState
	desiredIndex int
	lastAfter    string
	failStatus   int
	failCode     string
}

func (s *controlPlaneStub) server(t *testing.T) *httptest.Server {
	t.Helper()
	mux := http.NewServeMux()

	mux.HandleFunc("/v1/stamps/enroll", func(w http.ResponseWriter, r *http.Request) {
		s.enrollments.Add(1)
		if r.Header.Get("Authorization") != "" {
			t.Error("enrollment must not present a bearer credential")
		}
		writeJSON(w, 201, controlplane.EnrollResponse{
			Stamp: controlplane.Stamp{
				ID: stampID, AccountID: systemAcc, Mode: "managed", Status: "registered",
			},
			AgentCredential:     "fab_agent_" + stampID + "_secret",
			TelemetryCredential: "fab_telem_" + stampID + "_secret",
		})
	})

	mux.HandleFunc("/v1/stamps/"+stampID+"/heartbeat", func(w http.ResponseWriter, r *http.Request) {
		s.heartbeats.Add(1)
		requireAgentCredential(t, r)
		writeJSON(w, 200, map[string]any{"stamp_id": stampID})
	})

	mux.HandleFunc("/v1/stamps/"+stampID+"/desired-state", func(w http.ResponseWriter, r *http.Request) {
		requireAgentCredential(t, r)
		s.lastAfter = r.URL.Query().Get("after_generation")
		state := controlplane.DesiredState{StampID: stampID}
		if s.desiredIndex < len(s.desired) {
			state = s.desired[s.desiredIndex]
			s.desiredIndex++
		}
		writeJSON(w, 200, state)
	})

	mux.HandleFunc("/v1/stamps/"+stampID+"/status", func(w http.ResponseWriter, r *http.Request) {
		requireAgentCredential(t, r)
		if s.failStatus != 0 {
			writeJSON(w, s.failStatus, map[string]any{
				"error": map[string]string{"code": s.failCode, "message": "refused"},
			})
			return
		}
		report := controlplane.StatusReport{}
		if err := json.NewDecoder(r.Body).Decode(&report); err != nil {
			t.Errorf("decode status: %v", err)
		}
		s.statuses = append(s.statuses, report)
		writeJSON(w, 200, map[string]any{"deployment_id": report.DeploymentID})
	})

	server := httptest.NewServer(mux)
	t.Cleanup(server.Close)
	return server
}

func requireAgentCredential(t *testing.T, r *http.Request) {
	t.Helper()
	got := r.Header.Get("Authorization")
	if got == "" {
		t.Error("authenticated call is missing the agent credential")
	}
	// The telemetry credential belongs to the collector and must never appear here.
	if len(got) > 17 && got[7:17] == "fab_telem_" {
		t.Errorf("agent used the telemetry credential: %s", got)
	}
}

func writeJSON(w http.ResponseWriter, status int, body any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(body)
}

func newAgent(t *testing.T, url string) (*Agent, string) {
	t.Helper()
	dir := t.TempDir()
	config := Config{
		ControlPlaneURL:         url,
		EnrollmentToken:         "fab_enroll_token_secret",
		StampName:               "test-stamp",
		CredentialsPath:         filepath.Join(dir, "credentials.json"),
		DeploymentsPath:         filepath.Join(dir, "deployments.json"),
		TelemetryCredentialPath: filepath.Join(dir, state.TelemetryCredentialFile),
		UpstreamURL:             "http://model-host:8000",
	}
	return New(config, discardLogger()), dir
}

func assignment(id, account, alias string, generation int, release string) controlplane.DesiredDeployment {
	return controlplane.DesiredDeployment{
		DeploymentID:      id,
		AccountID:         account,
		Name:              alias,
		ModelAlias:        alias,
		DesiredGeneration: generation,
		Spec:              map[string]any{"runtime": map[string]any{"release": release}},
	}
}

func TestEnrollmentPersistsCredentialsOwnerOnly(t *testing.T) {
	stub := &controlPlaneStub{}
	server := stub.server(t)
	instance, dir := newAgent(t, server.URL)

	if err := instance.Ensure(context.Background()); err != nil {
		t.Fatalf("ensure: %v", err)
	}
	if instance.StampID() != stampID {
		t.Fatalf("stamp id = %q", instance.StampID())
	}

	path := filepath.Join(dir, "credentials.json")
	info, err := os.Stat(path)
	if err != nil {
		t.Fatalf("stat credentials: %v", err)
	}
	// The agent Secret must not be readable by the collector or operator.
	if perm := info.Mode().Perm(); perm != 0o600 {
		t.Errorf("credentials permissions = %o, want 600", perm)
	}

	saved, err := state.LoadCredentials(path)
	if err != nil {
		t.Fatalf("load credentials: %v", err)
	}
	if saved.TelemetryCredential == "" {
		t.Error("telemetry credential was not persisted for the collector")
	}
	if saved.AccountID != systemAcc {
		t.Errorf("stamp account = %q, want the system account", saved.AccountID)
	}
}

func TestRestartReusesCredentialsAndDoesNotEnrollTwice(t *testing.T) {
	stub := &controlPlaneStub{}
	server := stub.server(t)
	first, dir := newAgent(t, server.URL)
	if err := first.Ensure(context.Background()); err != nil {
		t.Fatalf("first ensure: %v", err)
	}

	// A second agent over the same state directory simulates a restart. Enrollment
	// tokens are single use, so enrolling again would strand the stamp.
	second := New(Config{
		ControlPlaneURL: server.URL,
		EnrollmentToken: "fab_enroll_token_secret",
		CredentialsPath: filepath.Join(dir, "credentials.json"),
		DeploymentsPath: filepath.Join(dir, "deployments.json"),
		UpstreamURL:     "http://model-host:8000",
	}, discardLogger())

	if err := second.Ensure(context.Background()); err != nil {
		t.Fatalf("second ensure: %v", err)
	}
	if got := stub.enrollments.Load(); got != 1 {
		t.Errorf("enrollments = %d, want 1", got)
	}
}

func TestEnrollmentRequiresATokenWhenNoStateExists(t *testing.T) {
	stub := &controlPlaneStub{}
	server := stub.server(t)
	dir := t.TempDir()
	instance := New(Config{
		ControlPlaneURL: server.URL,
		CredentialsPath: filepath.Join(dir, "credentials.json"),
		DeploymentsPath: filepath.Join(dir, "deployments.json"),
		UpstreamURL:     "http://model-host:8000",
	}, discardLogger())

	if err := instance.Ensure(context.Background()); err == nil {
		t.Fatal("expected an error when no credentials and no token are available")
	}
	if got := stub.enrollments.Load(); got != 0 {
		t.Errorf("enrollments = %d, want 0", got)
	}
}

func TestReconcileRendersOwningAccountsForAManagedStamp(t *testing.T) {
	stub := &controlPlaneStub{
		desired: []controlplane.DesiredState{{
			StampID:       stampID,
			MaxGeneration: 2,
			Deployments: []controlplane.DesiredDeployment{
				assignment(deployA, customerA, "alpha-model", 1, "release-a"),
				assignment(deployB, customerB, "beta-model", 2, "release-b"),
			},
		}},
	}
	server := stub.server(t)
	instance, dir := newAgent(t, server.URL)
	if err := instance.Ensure(context.Background()); err != nil {
		t.Fatalf("ensure: %v", err)
	}

	configured, err := instance.ReconcileOnce(context.Background())
	if err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	if len(configured) != 2 {
		t.Fatalf("configured %d deployments, want 2", len(configured))
	}

	file, err := state.ReadDeployments(filepath.Join(dir, "deployments.json"))
	if err != nil {
		t.Fatalf("read deployments: %v", err)
	}
	byAlias := map[string]state.Deployment{}
	for _, entry := range file.Deployments {
		byAlias[entry.ModelAlias] = entry
	}

	// Each entry carries its own customer account, not the stamp's system account.
	if byAlias["alpha-model"].AccountID != customerA {
		t.Errorf("alpha account = %q, want %q", byAlias["alpha-model"].AccountID, customerA)
	}
	if byAlias["beta-model"].AccountID != customerB {
		t.Errorf("beta account = %q, want %q", byAlias["beta-model"].AccountID, customerB)
	}
	for alias, entry := range byAlias {
		if entry.AccountID == systemAcc {
			t.Errorf("%s was rendered under the stamp's system account", alias)
		}
	}
	if byAlias["alpha-model"].UpstreamModel != "release-a" {
		t.Errorf("alpha upstream model = %q", byAlias["alpha-model"].UpstreamModel)
	}
}

func TestDeploymentsFileIsWorldReadableAndSorted(t *testing.T) {
	stub := &controlPlaneStub{
		desired: []controlplane.DesiredState{{
			MaxGeneration: 2,
			Deployments: []controlplane.DesiredDeployment{
				assignment(deployB, customerB, "zeta-model", 2, "r2"),
				assignment(deployA, customerA, "alpha-model", 1, "r1"),
			},
		}},
	}
	server := stub.server(t)
	instance, dir := newAgent(t, server.URL)
	_ = instance.Ensure(context.Background())
	if _, err := instance.ReconcileOnce(context.Background()); err != nil {
		t.Fatalf("reconcile: %v", err)
	}

	path := filepath.Join(dir, "deployments.json")
	info, err := os.Stat(path)
	if err != nil {
		t.Fatalf("stat: %v", err)
	}
	// The data plane reads this file and it holds no secret.
	if perm := info.Mode().Perm(); perm != 0o644 {
		t.Errorf("deployments permissions = %o, want 644", perm)
	}

	file, _ := state.ReadDeployments(path)
	if file.Deployments[0].ModelAlias != "alpha-model" {
		t.Errorf("entries are not sorted: %q first", file.Deployments[0].ModelAlias)
	}
}

func TestAcknowledgedGenerationAdvancesAndIsRequestedNextPass(t *testing.T) {
	stub := &controlPlaneStub{
		desired: []controlplane.DesiredState{
			{MaxGeneration: 3, Deployments: []controlplane.DesiredDeployment{
				assignment(deployA, customerA, "alpha-model", 3, "r1"),
			}},
			{MaxGeneration: 3},
		},
	}
	server := stub.server(t)
	instance, dir := newAgent(t, server.URL)
	_ = instance.Ensure(context.Background())

	if _, err := instance.ReconcileOnce(context.Background()); err != nil {
		t.Fatalf("first pass: %v", err)
	}
	saved, _ := state.LoadCredentials(filepath.Join(dir, "credentials.json"))
	if saved.AckedGeneration != 3 {
		t.Fatalf("acked generation = %d, want 3", saved.AckedGeneration)
	}

	if _, err := instance.ReconcileOnce(context.Background()); err != nil {
		t.Fatalf("second pass: %v", err)
	}
	// The second read must not replay generations already applied.
	if stub.lastAfter != "3" {
		t.Errorf("after_generation = %q, want 3", stub.lastAfter)
	}
}

func TestDeletedAssignmentIsWithdrawnFromConfiguration(t *testing.T) {
	stub := &controlPlaneStub{
		desired: []controlplane.DesiredState{
			{MaxGeneration: 1, Deployments: []controlplane.DesiredDeployment{
				assignment(deployA, customerA, "alpha-model", 1, "r1"),
			}},
			{MaxGeneration: 2, Deployments: []controlplane.DesiredDeployment{
				func() controlplane.DesiredDeployment {
					entry := assignment(deployA, customerA, "alpha-model", 2, "r1")
					entry.Deleted = true
					return entry
				}(),
			}},
		},
	}
	server := stub.server(t)
	instance, dir := newAgent(t, server.URL)
	_ = instance.Ensure(context.Background())

	if _, err := instance.ReconcileOnce(context.Background()); err != nil {
		t.Fatalf("first pass: %v", err)
	}
	configured, err := instance.ReconcileOnce(context.Background())
	if err != nil {
		t.Fatalf("second pass: %v", err)
	}
	if len(configured) != 0 {
		t.Fatalf("configured %d deployments after deletion, want 0", len(configured))
	}

	file, _ := state.ReadDeployments(filepath.Join(dir, "deployments.json"))
	if len(file.Deployments) != 0 {
		t.Errorf("deployments.json still lists %d entries", len(file.Deployments))
	}
	// Withdrawal is reported so the control plane learns the workload is gone.
	last := stub.statuses[len(stub.statuses)-1]
	if last.Phase != "terminating" || last.ReadyReplicas != 0 {
		t.Errorf("withdrawal status = %s ready=%d", last.Phase, last.ReadyReplicas)
	}
}

func TestStatusIsReportedWithObservedGeneration(t *testing.T) {
	stub := &controlPlaneStub{
		desired: []controlplane.DesiredState{{
			MaxGeneration: 7,
			Deployments: []controlplane.DesiredDeployment{
				assignment(deployA, customerA, "alpha-model", 7, "r1"),
			},
		}},
	}
	server := stub.server(t)
	instance, _ := newAgent(t, server.URL)
	_ = instance.Ensure(context.Background())
	if _, err := instance.ReconcileOnce(context.Background()); err != nil {
		t.Fatalf("reconcile: %v", err)
	}

	if len(stub.statuses) != 1 {
		t.Fatalf("status reports = %d, want 1", len(stub.statuses))
	}
	report := stub.statuses[0]
	if report.ObservedGeneration == nil || *report.ObservedGeneration != 7 {
		t.Errorf("observed generation = %v, want 7", report.ObservedGeneration)
	}
	if report.Phase != "ready" {
		t.Errorf("phase = %q", report.Phase)
	}
	// The condition must not imply a Kubernetes rollout the agent did not observe.
	if reason, _ := report.Conditions[0]["reason"].(string); reason != "AgentAppliedLocalConfiguration" {
		t.Errorf("condition reason = %q", reason)
	}
}

func TestStatusFailureDoesNotDiscardAppliedConfiguration(t *testing.T) {
	stub := &controlPlaneStub{
		failStatus: 400,
		failCode:   "placement_not_found",
		desired: []controlplane.DesiredState{{
			MaxGeneration: 1,
			Deployments: []controlplane.DesiredDeployment{
				assignment(deployA, customerA, "alpha-model", 1, "r1"),
			},
		}},
	}
	server := stub.server(t)
	instance, dir := newAgent(t, server.URL)
	_ = instance.Ensure(context.Background())

	configured, err := instance.ReconcileOnce(context.Background())
	if err != nil {
		t.Fatalf("a failed status write must not fail the pass: %v", err)
	}
	if len(configured) != 1 {
		t.Fatalf("configured %d deployments, want 1", len(configured))
	}
	if _, err := state.ReadDeployments(filepath.Join(dir, "deployments.json")); err != nil {
		t.Errorf("configuration was not written: %v", err)
	}
}

func TestHeartbeatRunsEveryPass(t *testing.T) {
	stub := &controlPlaneStub{desired: []controlplane.DesiredState{{}, {}}}
	server := stub.server(t)
	instance, _ := newAgent(t, server.URL)
	_ = instance.Ensure(context.Background())

	for i := 0; i < 2; i++ {
		if _, err := instance.ReconcileOnce(context.Background()); err != nil {
			t.Fatalf("pass %d: %v", i, err)
		}
	}
	if got := stub.heartbeats.Load(); got != 2 {
		t.Errorf("heartbeats = %d, want 2", got)
	}
}

func TestReconcileRequiresEnrollment(t *testing.T) {
	instance, _ := newAgent(t, "http://unused.invalid")
	if _, err := instance.ReconcileOnce(context.Background()); err == nil {
		t.Fatal("expected reconcile to refuse before enrollment")
	}
}

func TestEnrollmentHandsTheCollectorOnlyTheTelemetryCredential(t *testing.T) {
	// The collector's Secret must not contain the agent credential: that would let
	// it read desired state and write status, which the separation exists to stop.
	stub := &controlPlaneStub{}
	server := stub.server(t)
	instance, dir := newAgent(t, server.URL)

	if err := instance.Ensure(context.Background()); err != nil {
		t.Fatalf("ensure: %v", err)
	}

	path := filepath.Join(dir, state.TelemetryCredentialFile)
	written, err := state.ReadTelemetryCredential(path)
	if err != nil {
		t.Fatalf("read telemetry credential: %v", err)
	}

	credentials, err := state.LoadCredentials(filepath.Join(dir, "credentials.json"))
	if err != nil {
		t.Fatalf("load credentials: %v", err)
	}
	if written != credentials.TelemetryCredential {
		t.Fatalf("wrong credential handed over: %q", written)
	}
	if written == credentials.AgentCredential {
		t.Fatal("the agent credential must never be handed to the collector")
	}

	info, err := os.Stat(path)
	if err != nil {
		t.Fatalf("stat: %v", err)
	}
	// The collector's own Secret, not shared with the agent.
	if perm := info.Mode().Perm(); perm != 0o600 {
		t.Fatalf("telemetry credential is %o, want 0600", perm)
	}
}

func TestTheTelemetryHandOffIsOptional(t *testing.T) {
	// A stamp running no collector must still enrol.
	stub := &controlPlaneStub{}
	server := stub.server(t)
	dir := t.TempDir()
	instance := New(Config{
		ControlPlaneURL: server.URL,
		EnrollmentToken: "fab_enroll_token_secret",
		StampName:       "test-stamp",
		CredentialsPath: filepath.Join(dir, "credentials.json"),
		DeploymentsPath: filepath.Join(dir, "deployments.json"),
		UpstreamURL:     "http://model-host:8000",
	}, discardLogger())

	if err := instance.Ensure(context.Background()); err != nil {
		t.Fatalf("ensure: %v", err)
	}
	if _, err := os.Stat(filepath.Join(dir, state.TelemetryCredentialFile)); !os.IsNotExist(err) {
		t.Fatal("no telemetry credential file should exist when the hand-off is disabled")
	}
}
