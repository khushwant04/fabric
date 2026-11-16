package agent

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/khushwant04/fabric/agent/internal/activity"
	"github.com/khushwant04/fabric/agent/internal/controlplane"
	"github.com/khushwant04/fabric/agent/internal/hardware"
	"github.com/khushwant04/fabric/agent/internal/hostreport"
	"github.com/khushwant04/fabric/agent/internal/state"
)

func rtxProStamp() hardware.Capacity {
	return hardware.Capacity{
		Measured:     true,
		PodsMeasured: true,
		GPUs: []hardware.GPUGroup{{
			Product:           "NVIDIA RTX PRO 6000 Blackwell Server Edition",
			Count:             1,
			MemoryBytes:       97887 << 20,
			ComputeCapability: "12.0",
			Source:            "node label",
		}},
		AllocatableGPUs:    1,
		MaxGPUsPerNode:     1,
		MaxFreeGPUsPerNode: 1,
	}
}

func kinds(events []activity.Event) []string {
	out := make([]string, 0, len(events))
	for _, event := range events {
		out = append(out, event.Kind)
	}
	return out
}

func TestTheBootstrapStoryIsRecordedInOrder(t *testing.T) {
	// The owner's goal: "Enrolled → 1 × RTX PRO 6000 found (96 GiB) → GPU runtime OK → Ready".
	stub := &controlPlaneStub{}
	server := stub.server(t)
	instance, _ := newAgent(t, server.URL)
	instance.config.Capacity = &countingSource{capacity: rtxProStamp()}

	if err := instance.Ensure(context.Background()); err != nil {
		t.Fatalf("ensure: %v", err)
	}
	if _, err := instance.ReconcileOnce(context.Background()); err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	// A second pass measures nothing new and must not repeat the story.
	if _, err := instance.ReconcileOnce(context.Background()); err != nil {
		t.Fatalf("reconcile: %v", err)
	}

	events := instance.Activity().Pending()
	got := strings.Join(kinds(events), ",")
	if got != "enrolled,gpus_measured,gpu_runtime,ready" {
		t.Fatalf("kinds = %s", got)
	}
	if events[0].Message != "Enrolled with the control plane as test-stamp" {
		t.Errorf("enrolled message = %q", events[0].Message)
	}
	if events[1].Message != "1 × NVIDIA RTX PRO 6000 Blackwell Server Edition found (96 GiB)" {
		t.Errorf("gpus message = %q", events[1].Message)
	}
	if events[1].Details["count"] != 1 || events[1].Details["memory_gib"] != 95.6 {
		t.Errorf("gpus details = %v", events[1].Details)
	}
	if events[2].Severity != activity.Success || !strings.Contains(events[2].Message, "GPU runtime OK") {
		t.Errorf("runtime = %+v", events[2])
	}
	if events[3].Message != "Ready for deployments" {
		t.Errorf("ready = %+v", events[3])
	}
}

func TestAMissingGPURuntimeWarnsWithAHintAndWithholdsReady(t *testing.T) {
	stub := &controlPlaneStub{}
	server := stub.server(t)
	instance, _ := newAgent(t, server.URL)
	instance.config.Capacity = &countingSource{capacity: hardware.Capacity{
		Measured: true, GPUs: []hardware.GPUGroup{}, UnequippedGPUNodes: 1,
	}}
	if err := instance.Ensure(context.Background()); err != nil {
		t.Fatalf("ensure: %v", err)
	}
	if _, err := instance.ReconcileOnce(context.Background()); err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	var runtime *activity.Event
	for _, event := range instance.Activity().Pending() {
		if event.Kind == activity.KindReady {
			t.Fatal("a stamp without a GPU runtime is not ready for deployments")
		}
		if event.Kind == activity.KindGPURuntime {
			copied := event
			runtime = &copied
		}
	}
	if runtime == nil || runtime.Severity != activity.Warning {
		t.Fatalf("runtime event = %+v", runtime)
	}
	if !strings.Contains(runtime.Message, "1 GPU node(s) advertise no allocatable nvidia.com/gpu") ||
		!strings.Contains(runtime.Hint, "re-run the bootstrap command") {
		t.Errorf("runtime = %+v", runtime)
	}
}

func TestRotationAndAppliedGenerationsAreRecorded(t *testing.T) {
	stub := &controlPlaneStub{
		desired: []controlplane.DesiredState{{
			StampID: stampID, MaxGeneration: 1,
			Deployments: []controlplane.DesiredDeployment{
				assignment(deployA, customerA, "qwen-chat", 1, "Qwen/Qwen3.5-2B"),
			},
		}},
	}
	stub.requestRotation.Store(true)
	server := stub.server(t)
	instance, _ := newAgent(t, server.URL)
	if err := instance.Ensure(context.Background()); err != nil {
		t.Fatalf("ensure: %v", err)
	}
	if _, err := instance.ReconcileOnce(context.Background()); err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	var rotated, applied bool
	for _, event := range instance.Activity().Pending() {
		switch event.Kind {
		case activity.KindCredentialsRotated:
			rotated = strings.Contains(event.Message, "version 2, requested from the console")
			if strings.Contains(event.Message, "fab_agent_") {
				t.Error("a credential reached the activity log")
			}
		case activity.KindDesiredStateApplied:
			applied = event.Message == "Applied desired-state generation 1: configured qwen-chat"
		}
	}
	if !rotated || !applied {
		t.Fatalf("rotated=%v applied=%v events=%v", rotated, applied, kinds(instance.Activity().Pending()))
	}
}

func TestImagePullsAndFailuresComeFromHostReports(t *testing.T) {
	instance := New(Config{ControlPlaneURL: "http://unused"}, discardLogger())
	instance.infos.Store(map[string]deploymentInfo{deployA: {Alias: "qwen-chat"}})
	at := time.Date(2026, 11, 16, 10, 0, 0, 0, time.UTC)
	report := hostreport.Report{Deployments: []hostreport.Deployment{{
		DeploymentID: deployA,
		Pods: []hostreport.Pod{{
			Name: "fabric-host-a", Phase: "Running", ContainerState: "waiting",
			ContainerReason: "CrashLoopBackOff", RestartCount: 2,
			Log: "RuntimeError: Failed to infer device type",
			Events: []hostreport.Event{
				{Type: "Normal", Reason: "Pulling", Message: `Pulling image "registry/model-host:v1"`, At: at},
				{Type: "Normal", Reason: "Pulled", At: at.Add(274 * time.Second), Message: `Successfully pulled image "registry/model-host:v1" in 4m34.2s (4m34.2s including waiting). Image size: 8912345678 bytes.`},
			},
		}},
	}}}
	instance.observeHosts(report)
	instance.observeHosts(report) // the same events, re-read on the next report

	events := instance.Activity().Pending()
	if got := strings.Join(kinds(events), ","); got != "image_pull_started,image_pull_finished,model_host_failed" {
		t.Fatalf("kinds = %s", got)
	}
	if !strings.Contains(events[0].Message, "qwen-chat") || !strings.Contains(events[0].Hint, "downloads the model runtime once") {
		t.Errorf("pull started = %+v", events[0])
	}
	if events[1].Message != "Model runtime image pulled in 4m34s (8.9 GB)" {
		t.Errorf("pull finished = %q", events[1].Message)
	}
	if !events[1].At.Equal(at.Add(274 * time.Second)) {
		t.Errorf("pull finished at %v", events[1].At)
	}
	if events[2].Details["reason"] != "GPURuntimeMissing" || !strings.Contains(events[2].Hint, "bootstrap") {
		t.Errorf("failure = %+v", events[2])
	}
}

func TestActivityIsUploadedWithTheAgentCredential(t *testing.T) {
	var mu sync.Mutex
	var path, authorization string
	var body struct {
		Events []activity.Event `json:"events"`
	}
	received := make(chan struct{}, 1)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		path, authorization = r.URL.Path, r.Header.Get("Authorization")
		raw, _ := io.ReadAll(r.Body)
		_ = json.Unmarshal(raw, &body)
		mu.Unlock()
		writeJSON(w, 200, map[string]any{"accepted": 1})
		select {
		case received <- struct{}{}:
		default:
		}
	}))
	defer server.Close()

	instance := New(Config{ControlPlaneURL: server.URL, ReportActivity: true}, discardLogger())
	instance.credentials = &state.Credentials{StampID: stampID}
	instance.setAgentCredential("fab_agent_current")
	instance.recordEnrolled(stampID, "rtx-pro-6000", "byoi")
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	instance.startActivityReporter(ctx)
	select {
	case <-received:
	case <-time.After(5 * time.Second):
		t.Fatal("activity was never uploaded")
	}
	mu.Lock()
	defer mu.Unlock()
	if path != "/v1/stamps/"+stampID+"/events" || authorization != "Bearer fab_agent_current" {
		t.Errorf("path=%s auth=%s", path, authorization)
	}
	if len(body.Events) != 1 || body.Events[0].Kind != "enrolled" || body.Events[0].ID == "" {
		t.Errorf("body = %+v", body)
	}
}

func TestWeightProgressIsDroppedForAControlPlaneThatRejectsIt(t *testing.T) {
	var mu sync.Mutex
	var bodies []string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		raw, _ := io.ReadAll(r.Body)
		mu.Lock()
		bodies = append(bodies, string(raw))
		mu.Unlock()
		if strings.Contains(string(raw), `"weights"`) {
			writeJSON(w, 422, map[string]any{"detail": "extra fields not permitted"})
			return
		}
		writeJSON(w, 200, map[string]any{})
	}))
	defer server.Close()

	report := hostreport.Report{Deployments: []hostreport.Deployment{{DeploymentID: deployA, Pods: []hostreport.Pod{{
		Name: "fabric-host-a", Weights: &hostreport.WeightProgress{Repository: "Qwen/Qwen3.5-2B", DownloadedBytes: 1},
	}}}}}
	reporter := &oneShotReporter{report: report, result: make(chan error, 1)}
	instance := New(Config{ControlPlaneURL: server.URL, HostReporter: reporter}, discardLogger())
	instance.credentials = &state.Credentials{StampID: stampID}
	instance.setAgentCredential("fab_agent_current")
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	instance.startHostReporter(ctx)
	if err := <-reporter.result; err != nil {
		t.Fatalf("send: %v", err)
	}
	mu.Lock()
	defer mu.Unlock()
	if len(bodies) != 2 || strings.Contains(bodies[1], `"weights"`) {
		t.Fatalf("bodies = %v", bodies)
	}
	if !instance.weightsRejected.Load() {
		t.Error("the refusal was not remembered")
	}
}
