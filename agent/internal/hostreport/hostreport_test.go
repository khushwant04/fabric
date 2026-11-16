package hostreport

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/url"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

// fakeCluster is a fake clientset for the three reads the reporter makes: the pod list,
// each pod's events, and each pod's log. It records every path so the selectors and log
// parameters, which are part of the contract with the API server, can be asserted.
type fakeCluster struct {
	mu      sync.Mutex
	pods    string
	podErr  error
	events  map[string]string // pod name -> event list JSON
	logs    map[string]string // pod name -> log text
	logErr  map[string]error
	paths   []string
	logURLs []*url.URL
}

func (f *fakeCluster) Get(_ context.Context, path string, out any) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.paths = append(f.paths, path)
	parsed, err := url.Parse(path)
	if err != nil {
		return err
	}
	switch {
	case strings.HasSuffix(parsed.Path, "/pods"):
		if f.podErr != nil {
			return f.podErr
		}
		return json.Unmarshal([]byte(f.pods), out)
	case strings.HasSuffix(parsed.Path, "/events"):
		selector := parsed.Query().Get("fieldSelector")
		name := selector[strings.LastIndex(selector, "=")+1:]
		payload, ok := f.events[name]
		if !ok {
			payload = `{"items":[]}`
		}
		return json.Unmarshal([]byte(payload), out)
	}
	return fmt.Errorf("unexpected path %s", path)
}

func (f *fakeCluster) GetText(_ context.Context, path string, _ int64) ([]byte, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.paths = append(f.paths, path)
	parsed, err := url.Parse(path)
	if err != nil {
		return nil, err
	}
	f.logURLs = append(f.logURLs, parsed)
	segments := strings.Split(parsed.Path, "/")
	name := segments[len(segments)-2]
	if err := f.logErr[name]; err != nil {
		return nil, err
	}
	return []byte(f.logs[name]), nil
}

const (
	deploymentA = "0b7e7c1e-0000-4000-8000-00000000000a"
	deploymentB = "0b7e7c1e-0000-4000-8000-00000000000b"
)

func podJSON(name, deployment, phase string, ready bool, containerState string, restarts int) string {
	readyStatus := "False"
	if ready {
		readyStatus = "True"
	}
	return fmt.Sprintf(`{
		"metadata":{"name":%q,"creationTimestamp":"2026-11-16T10:00:00Z",
			"labels":{"app.kubernetes.io/managed-by":"fabric-operator",
				"fabric.khushwant.dev/deployment-id":%q}},
		"status":{"phase":%q,"startTime":"2026-11-16T10:00:01Z",
			"conditions":[{"type":"PodScheduled","status":"True"},
				{"type":"Ready","status":%q,"reason":"ContainersNotReady"}],
			"containerStatuses":[{"name":"model-host","restartCount":%d,"state":%s}]}}`,
		name, deployment, phase, readyStatus, restarts, containerState)
}

func list(items ...string) string {
	return `{"items":[` + strings.Join(items, ",") + `]}`
}

func newReporter(cluster *fakeCluster) *Reporter {
	return &Reporter{
		Kube:      cluster,
		Namespace: "fabric-stamp",
		Now:       func() time.Time { return time.Date(2026, 11, 16, 10, 5, 0, 0, time.UTC) },
	}
}

func TestCollectReportsEachHostWithStatusEventsAndLogTail(t *testing.T) {
	pulling := podJSON("fabric-host-a-1", deploymentA, "Pending", false,
		`{"waiting":{"reason":"ContainerCreating"}}`, 0)
	loading := podJSON("fabric-host-a-2", deploymentA, "Running", false,
		`{"running":{"startedAt":"2026-11-16T10:01:00Z"}}`, 0)
	ready := podJSON("fabric-host-b-1", deploymentB, "Running", true,
		`{"running":{"startedAt":"2026-11-16T09:00:00Z"}}`, 0)
	cluster := &fakeCluster{
		pods: list(pulling, loading, ready),
		events: map[string]string{"fabric-host-a-1": `{"items":[
			{"type":"Normal","reason":"Pulling","message":"Pulling image \"vllm\"","count":1,
			 "lastTimestamp":"2026-11-16T10:00:05Z"},
			{"type":"Normal","reason":"Scheduled","message":"Successfully assigned","count":1,
			 "lastTimestamp":"2026-11-16T10:00:01Z"}]}`},
		logs: map[string]string{
			"fabric-host-a-2": "INFO Loading weights\nINFO Loading safetensors 3/8\n",
			"fabric-host-b-1": "INFO Application startup complete.\n",
		},
	}

	report, err := newReporter(cluster).Collect(context.Background())
	if err != nil {
		t.Fatalf("collect: %v", err)
	}

	// The pod list is namespaced and selects only the operator's model hosts.
	listPath, _ := url.Parse(cluster.paths[0])
	if listPath.Path != "/api/v1/namespaces/fabric-stamp/pods" {
		t.Fatalf("pod list path = %s", listPath.Path)
	}
	if got := listPath.Query().Get("labelSelector"); got !=
		"app.kubernetes.io/managed-by=fabric-operator,fabric.khushwant.dev/deployment-id" {
		t.Fatalf("label selector = %q", got)
	}

	if len(report.Deployments) != 2 || report.Deployments[0].DeploymentID != deploymentA {
		t.Fatalf("deployments = %+v", report.Deployments)
	}
	pods := report.Deployments[0].Pods
	if len(pods) != 2 || pods[0].Name != "fabric-host-a-1" {
		t.Fatalf("pods = %+v", pods)
	}

	first := pods[0]
	if first.ContainerState != "waiting" || first.ContainerReason != "ContainerCreating" {
		t.Errorf("pulling pod state = %q/%q", first.ContainerState, first.ContainerReason)
	}
	// A container that has not started has no log to read, and asking would only earn a
	// 400 from the API server on every pass.
	if first.Log != "" || first.LogError == "" {
		t.Errorf("pulling pod log = %q, error %q", first.Log, first.LogError)
	}
	if len(first.Events) != 2 || first.Events[0].Reason != "Scheduled" ||
		first.Events[1].Reason != "Pulling" {
		t.Errorf("events not in time order: %+v", first.Events)
	}

	second := pods[1]
	if second.Ready || second.ContainerState != "running" {
		t.Errorf("loading pod = %+v", second)
	}
	if second.Log != "INFO Loading weights\nINFO Loading safetensors 3/8" {
		t.Errorf("log = %q", second.Log)
	}

	if !report.Deployments[1].Pods[0].Ready {
		t.Errorf("ready pod not reported ready")
	}
	if report.Settled() {
		t.Errorf("a report with hosts still starting must not be settled")
	}

	// Logs are read from the model server's container, bounded by line count.
	if len(cluster.logURLs) != 2 {
		t.Fatalf("log reads = %d, want 2 (the pulling pod has none)", len(cluster.logURLs))
	}
	for _, read := range cluster.logURLs {
		query := read.Query()
		if query.Get("container") != "model-host" || query.Get("tailLines") != "200" ||
			query.Get("previous") != "" {
			t.Errorf("log query = %s", read.RawQuery)
		}
	}
}

func TestACrashLoopingHostReportsThePreviousInstancesLog(t *testing.T) {
	crashing := podJSON("fabric-host-a-1", deploymentA, "Running", false,
		`{"waiting":{"reason":"CrashLoopBackOff","message":"back-off 40s restarting failed container"}}`, 3)
	cluster := &fakeCluster{
		pods: list(crashing),
		logs: map[string]string{"fabric-host-a-1": "Traceback (most recent call last):\n" +
			"torch.OutOfMemoryError: CUDA out of memory\n"},
	}

	report, err := newReporter(cluster).Collect(context.Background())
	if err != nil {
		t.Fatalf("collect: %v", err)
	}
	pod := report.Deployments[0].Pods[0]
	if pod.RestartCount != 3 || pod.ContainerReason != "CrashLoopBackOff" {
		t.Fatalf("pod = %+v", pod)
	}
	if !pod.LogPrevious || !strings.Contains(pod.Log, "CUDA out of memory") {
		t.Fatalf("expected the previous instance's traceback, got %q (previous=%v)",
			pod.Log, pod.LogPrevious)
	}
	if got := cluster.logURLs[0].Query().Get("previous"); got != "true" {
		t.Fatalf("previous = %q", got)
	}
}

func TestAnUnreadableLogStillReportsThePod(t *testing.T) {
	running := podJSON("fabric-host-a-1", deploymentA, "Running", true, `{"running":{}}`, 0)
	cluster := &fakeCluster{
		pods:   list(running),
		logErr: map[string]error{"fabric-host-a-1": errors.New("forbidden")},
	}
	report, err := newReporter(cluster).Collect(context.Background())
	if err != nil {
		t.Fatalf("collect: %v", err)
	}
	pod := report.Deployments[0].Pods[0]
	if !pod.Ready || pod.LogError == "" || pod.Log != "" {
		t.Fatalf("pod = %+v", pod)
	}
}

func TestAFailedPodListingSendsNothing(t *testing.T) {
	// An empty report means "every host is gone" to the control plane, which deletes the
	// snapshots. A listing that failed must never be mistaken for that.
	cluster := &fakeCluster{podErr: errors.New("pods is forbidden")}
	reporter := newReporter(cluster)
	reporter.ActiveInterval = time.Millisecond
	reporter.SteadyInterval = time.Millisecond

	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Millisecond)
	defer cancel()
	var sends atomic.Int32
	reporter.Run(ctx, func(context.Context, Report) error { sends.Add(1); return nil })
	if sends.Load() != 0 {
		t.Fatalf("sent %d reports from a failed listing", sends.Load())
	}
}

func TestReportsAreFrequentWhileStartingAndSlowOnceReady(t *testing.T) {
	run := func(pods string) int32 {
		cluster := &fakeCluster{pods: pods, logs: map[string]string{}}
		reporter := newReporter(cluster)
		reporter.ActiveInterval = 2 * time.Millisecond
		reporter.SteadyInterval = time.Hour
		ctx, cancel := context.WithTimeout(context.Background(), 60*time.Millisecond)
		defer cancel()
		var sends atomic.Int32
		reporter.Run(ctx, func(context.Context, Report) error { sends.Add(1); return nil })
		return sends.Load()
	}

	starting := run(list(podJSON("fabric-host-a-1", deploymentA, "Pending", false,
		`{"waiting":{"reason":"ContainerCreating"}}`, 0)))
	if starting < 3 {
		t.Errorf("starting host reported %d times, want frequent reports", starting)
	}
	ready := run(list(podJSON("fabric-host-a-1", deploymentA, "Running", true, `{"running":{}}`, 0)))
	if ready != 1 {
		t.Errorf("ready host reported %d times, want one per steady interval", ready)
	}
	// No hosts at all is settled too: the report that clears the last snapshots is sent,
	// and then the reporter idles.
	if empty := run(list()); empty != 1 {
		t.Errorf("empty stamp reported %d times", empty)
	}
}

func TestANotAcceptedReportBacksOff(t *testing.T) {
	cluster := &fakeCluster{pods: list(podJSON("fabric-host-a-1", deploymentA, "Pending", false,
		`{"waiting":{"reason":"ContainerCreating"}}`, 0))}
	reporter := newReporter(cluster)
	reporter.ActiveInterval = time.Millisecond
	reporter.SteadyInterval = time.Millisecond
	ctx, cancel := context.WithTimeout(context.Background(), 40*time.Millisecond)
	defer cancel()
	var sends atomic.Int32
	reporter.Run(ctx, func(context.Context, Report) error {
		sends.Add(1)
		return fmt.Errorf("%w: 404", ErrNotAccepted)
	})
	if sends.Load() != 1 {
		t.Fatalf("sent %d times to a control plane without the endpoint", sends.Load())
	}
}

func TestSanitizeDropsCredentialLinesAndTerminalNoise(t *testing.T) {
	raw := strings.Join([]string{
		"\x1b[32mINFO\x1b[0m 11-16 10:00:00 Starting vLLM API server",
		"HF_TOKEN=hf_abcdefghijklmnopqrstuvwxyz0123",
		"curl -H 'Authorization: Bearer abc.def.ghi'",
		"Loading safetensors:  10%\rLoading safetensors:  50%\rLoading safetensors: 100%",
		"api_key = sk-proj-aaaaaaaaaaaaaaaaaaaaaaaaaaaa",
		"INFO Application startup complete.",
	}, "\n")

	clean, truncated := Sanitize(raw, MaxLogBytes)
	if truncated {
		t.Fatalf("small log reported truncated")
	}
	lines := strings.Split(clean, "\n")
	want := []string{
		"INFO 11-16 10:00:00 Starting vLLM API server",
		RedactedLine,
		RedactedLine,
		"Loading safetensors: 100%",
		RedactedLine,
		"INFO Application startup complete.",
	}
	if len(lines) != len(want) {
		t.Fatalf("lines = %q", lines)
	}
	for index := range want {
		if lines[index] != want[index] {
			t.Errorf("line %d = %q, want %q", index, lines[index], want[index])
		}
	}
	if strings.Contains(clean, "hf_") || strings.Contains(clean, "abc.def") {
		t.Fatalf("a credential survived: %q", clean)
	}
}

func TestSanitizeKeepsTheNewestLinesWithinTheBudget(t *testing.T) {
	var builder strings.Builder
	for index := 0; index < 100; index++ {
		fmt.Fprintf(&builder, "line %03d %s\n", index, strings.Repeat("x", 90))
	}
	clean, truncated := Sanitize(builder.String(), 1000)
	if !truncated {
		t.Fatalf("expected truncation")
	}
	if len(clean) > 1000 {
		t.Fatalf("kept %d bytes, budget 1000", len(clean))
	}
	if !strings.HasSuffix(clean, "line 099 "+strings.Repeat("x", 90)) {
		t.Fatalf("newest line missing: %q", clean[len(clean)-120:])
	}
	if !strings.HasPrefix(clean, "line ") {
		t.Fatalf("cut mid-line: %q", clean[:20])
	}

	long, _ := Sanitize(strings.Repeat("y", MaxLineBytes*2), MaxLogBytes)
	if len(long) > MaxLineBytes {
		t.Fatalf("a single line was not capped: %d bytes", len(long))
	}
}

func TestEventsAreCappedToTheMostRecent(t *testing.T) {
	var items []string
	base := time.Date(2026, 11, 16, 10, 0, 0, 0, time.UTC)
	for index := 0; index < MaxEvents+5; index++ {
		items = append(items, fmt.Sprintf(
			`{"type":"Warning","reason":"Unhealthy","message":"probe %d","count":2,"lastTimestamp":%q}`,
			index, base.Add(time.Duration(index)*time.Second).Format(time.RFC3339)))
	}
	cluster := &fakeCluster{
		pods:   list(podJSON("fabric-host-a-1", deploymentA, "Running", false, `{"running":{}}`, 0)),
		events: map[string]string{"fabric-host-a-1": list(items...)},
		logs:   map[string]string{},
	}
	report, err := newReporter(cluster).Collect(context.Background())
	if err != nil {
		t.Fatalf("collect: %v", err)
	}
	events := report.Deployments[0].Pods[0].Events
	if len(events) != MaxEvents {
		t.Fatalf("events = %d, want %d", len(events), MaxEvents)
	}
	if events[len(events)-1].Message != fmt.Sprintf("probe %d", MaxEvents+4) ||
		events[0].Message != "probe 5" {
		t.Fatalf("kept the wrong events: first %q last %q", events[0].Message,
			events[len(events)-1].Message)
	}
	if events[0].Count != 2 || events[0].Type != "Warning" {
		t.Fatalf("event = %+v", events[0])
	}
}
