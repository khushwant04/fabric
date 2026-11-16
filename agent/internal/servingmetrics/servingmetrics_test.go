package servingmetrics

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"math"
	"net"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"
)

const (
	deploymentA = "11111111-1111-4111-8111-111111111111"
	deploymentB = "22222222-2222-4222-8222-222222222222"
)

// text serves a Prometheus exposition that the test can change between scrapes.
type text struct {
	mu   sync.Mutex
	body string
	down bool
}

func (t *text) set(body string) { t.mu.Lock(); t.body = body; t.mu.Unlock() }

func (t *text) ServeHTTP(w http.ResponseWriter, _ *http.Request) {
	t.mu.Lock()
	defer t.mu.Unlock()
	if t.down {
		http.Error(w, "loading", http.StatusServiceUnavailable)
		return
	}
	_, _ = io.WriteString(w, t.body)
}

func serve(t *testing.T, handler http.Handler) (*httptest.Server, string, int) {
	t.Helper()
	server := httptest.NewServer(handler)
	t.Cleanup(server.Close)
	host, port, _ := net.SplitHostPort(strings.TrimPrefix(server.URL, "http://"))
	number, _ := strconv.Atoi(port)
	return server, host, number
}

// fakeKube answers the two reads the reporter makes.
type fakeKube struct {
	pods      string
	endpoints map[string]string
	fail      bool
}

func (f *fakeKube) Get(_ context.Context, path string, out any) error {
	if f.fail {
		return errors.New("forbidden")
	}
	if strings.Contains(path, "/pods?") {
		return json.Unmarshal([]byte(f.pods), out)
	}
	for suffix, body := range f.endpoints {
		if strings.HasSuffix(path, suffix) {
			return json.Unmarshal([]byte(body), out)
		}
	}
	return errors.New("not found")
}

func podJSON(name, deployment, node, ip string, port int) string {
	return fmt.Sprintf(`{"metadata":{"name":%q,"labels":{"app.kubernetes.io/managed-by":"fabric-operator",
		"fabric.khushwant.dev/deployment-id":%q}},"spec":{"nodeName":%q,"containers":[{"name":"model-host",
		"ports":[{"name":"http","containerPort":%d}]}]},"status":{"phase":"Running","podIP":%q}}`,
		name, deployment, node, port, ip)
}

func gatewayText(ok, limited, failed, output float64, fast, slow float64) string {
	return fmt.Sprintf(`# HELP fabric_dp_requests_total Requests handled, by outcome.
# TYPE fabric_dp_requests_total counter
fabric_dp_requests_total{deployment_id="%[1]s",account_id="acct",outcome="ok"} %[2]g
fabric_dp_requests_total{deployment_id="%[1]s",account_id="acct",outcome="rate_limited"} %[3]g
fabric_dp_requests_total{deployment_id="%[1]s",account_id="acct",outcome="upstream_unavailable"} %[4]g
fabric_dp_requests_total{deployment_id="unknown",account_id="acct",outcome="ok"} 99
fabric_dp_tokens_total{deployment_id="%[1]s",direction="input"} 1000
fabric_dp_tokens_total{deployment_id="%[1]s",direction="output"} %[5]g
fabric_dp_request_duration_seconds_bucket{deployment_id="%[1]s",le="0.5"} %[6]g
fabric_dp_request_duration_seconds_bucket{deployment_id="%[1]s",le="1.0"} %[6]g
fabric_dp_request_duration_seconds_bucket{deployment_id="%[1]s",le="5.0"} %[7]g
fabric_dp_request_duration_seconds_bucket{deployment_id="%[1]s",le="+Inf"} %[7]g
fabric_dp_request_duration_seconds_sum{deployment_id="%[1]s"} 12
fabric_dp_request_duration_seconds_count{deployment_id="%[1]s"} %[7]g
`, deploymentA, ok, limited, failed, output, fast, fast+slow)
}

func vllmText(running, waiting, kv, generated, ttftFast, ttftSlow float64) string {
	return fmt.Sprintf(`# TYPE vllm:num_requests_running gauge
vllm:num_requests_running{engine="0",model_name="qwen"} %g
vllm:num_requests_waiting{engine="0",model_name="qwen"} %g
vllm:kv_cache_usage_perc{engine="0",model_name="qwen"} %g
vllm:generation_tokens_total{engine="0",model_name="qwen"} %g
vllm:time_to_first_token_seconds_bucket{engine="0",le="0.1",model_name="qwen"} %g
vllm:time_to_first_token_seconds_bucket{engine="0",le="0.5",model_name="qwen"} %g
vllm:time_to_first_token_seconds_bucket{engine="0",le="+Inf",model_name="qwen"} %g
vllm:inter_token_latency_seconds_bucket{engine="0",le="0.025",model_name="qwen"} 10
vllm:inter_token_latency_seconds_bucket{engine="0",le="+Inf",model_name="qwen"} 10
`, running, waiting, kv, generated, ttftFast, ttftFast+ttftSlow, ttftFast+ttftSlow)
}

const dcgmText = `# HELP DCGM_FI_DEV_GPU_UTIL GPU utilization (in %%).
DCGM_FI_DEV_GPU_UTIL{gpu="0",UUID="GPU-a",device="nvidia0",modelName="NVIDIA A10",Hostname="x",container="model-host",namespace="fabric-stamp",pod="host-a-1"} 87
DCGM_FI_DEV_FB_USED{gpu="0",UUID="GPU-a",device="nvidia0",modelName="NVIDIA A10",Hostname="x",container="model-host",namespace="fabric-stamp",pod="host-a-1"} 20000
DCGM_FI_DEV_FB_FREE{gpu="0",UUID="GPU-a",device="nvidia0",modelName="NVIDIA A10",Hostname="x",container="model-host",namespace="fabric-stamp",pod="host-a-1"} 3028
DCGM_FI_DEV_GPU_UTIL{gpu="1",UUID="GPU-b",device="nvidia1",modelName="NVIDIA A10",Hostname="x",container="other",namespace="someone-else",pod="theirs"} 40
DCGM_FI_DEV_FB_USED{gpu="1",UUID="GPU-b",device="nvidia1",modelName="NVIDIA A10",Hostname="x"} 1000
DCGM_FI_DEV_FB_FREE{gpu="1",UUID="GPU-b",device="nvidia1",modelName="NVIDIA A10",Hostname="x"} 22028
`

func quiet() *slog.Logger { return slog.New(slog.NewTextHandler(io.Discard, nil)) }

type clock struct{ at time.Time }

func (c *clock) now() time.Time { return c.at }

func TestParseExpositionReadsLabelsEscapesAndTimestamps(t *testing.T) {
	body := `# comment
metric_a{path="/v1/x",quote="say \"hi\"",empty=""} 3 1700000000000
metric_a 4
metric_b{x="y"} NaN
metric_c{broken="no-close} 1
`
	samples := parseExposition(strings.NewReader(body), func(name string) bool { return true })
	if len(samples) != 2 {
		t.Fatalf("samples = %+v", samples)
	}
	if samples[0].Labels["quote"] != `say "hi"` || samples[0].Labels["path"] != "/v1/x" || samples[0].Value != 3 {
		t.Fatalf("first = %+v", samples[0])
	}
	if samples[1].Value != 4 || len(samples[1].Labels) != 0 {
		t.Fatalf("second = %+v", samples[1])
	}
}

func TestHistogramDeltaAndQuantile(t *testing.T) {
	previous := cumulative{"0.1": 10, "0.5": 15, "+Inf": 16}
	current := cumulative{"0.1": 20, "0.5": 35, "+Inf": 40}
	delta := histogramDelta(current, previous)
	want := Histogram{"0.1": 10, "0.5": 10, "+Inf": 4}
	for bound, count := range want {
		if delta[bound] != count {
			t.Fatalf("delta = %v, want %v", delta, want)
		}
	}
	median, ok := delta.Quantile(0.5)
	if !ok || math.Abs(median-0.18) > 1e-9 { // rank 12: 2 of 10 into (0.1, 0.5]
		t.Fatalf("p50 = %v", median)
	}
	p95, _ := delta.Quantile(0.95)
	if p95 != 0.5 { // in the +Inf bucket: the largest finite bound
		t.Fatalf("p95 = %v", p95)
	}
	p70, _ := delta.Quantile(0.7) // rank 16.8: 6.8 of 10 into (0.1, 0.5]
	if math.Abs(p70-(0.1+0.4*0.68)) > 1e-9 {
		t.Fatalf("p70 = %v", p70)
	}

	// A restarted process: the current scrape is the whole interval.
	reset := histogramDelta(cumulative{"0.1": 2, "+Inf": 3}, previous)
	if reset["0.1"] != 2 || reset["+Inf"] != 1 {
		t.Fatalf("reset delta = %v", reset)
	}
	if histogramDelta(previous, previous) != nil {
		t.Fatal("no observations should be no histogram")
	}
	if _, ok := Histogram(nil).Quantile(0.5); ok {
		t.Fatal("empty histogram has no quantile")
	}
}

func TestStatusClasses(t *testing.T) {
	cases := map[string]string{
		"ok": "2xx", "ok_streamed": "2xx", "cancelled": "cancelled", "cancelled_streamed": "cancelled",
		"upstream_error": "5xx", "upstream_error_streamed": "5xx", "upstream_unavailable": "5xx",
		"limit_coordinator_unavailable": "5xx", "rate_limited": "4xx", "concurrency_limited": "4xx",
	}
	for outcome, want := range cases {
		if got := StatusClass(outcome); got != want {
			t.Errorf("StatusClass(%q) = %q, want %q", outcome, got, want)
		}
	}
}

func TestCollectAggregatesTheGatewayModelHostsAndGPUs(t *testing.T) {
	gateway := &text{body: gatewayText(100, 2, 1, 5000, 90, 10)}
	gatewayServer, _, _ := serve(t, gateway)
	hostA := &text{body: vllmText(3, 1, 0.40, 10000, 50, 0)}
	_, ipA, portA := serve(t, hostA)
	hostB := &text{body: vllmText(5, 0, 0.60, 20000, 10, 0)}
	_, ipB, portB := serve(t, hostB)
	dcgm := &text{body: dcgmText}
	_, ipD, portD := serve(t, dcgm)

	kube := &fakeKube{
		pods: `{"items":[` + podJSON("host-a-1", deploymentA, "gpu-node-1", ipA, portA) + "," +
			podJSON("host-a-2", deploymentA, "gpu-node-2", ipB, portB) + `]}`,
		endpoints: map[string]string{
			"/namespaces/fabric-observability/endpoints/dcgm-dcgm-exporter": fmt.Sprintf(
				`{"subsets":[{"addresses":[{"ip":%q,"nodeName":"gpu-node-1"}],"ports":[{"name":"metrics","port":%d}]}]}`,
				ipD, portD),
		},
	}
	at := &clock{at: time.Date(2026, 11, 16, 10, 0, 0, 0, time.UTC)}
	reporter := &Reporter{
		Kube: kube, Namespace: "fabric-stamp", GatewayURL: gatewayServer.URL + "/metrics",
		DCGM: []DCGMService{{Namespace: "fabric-observability", Name: "dcgm-dcgm-exporter"}},
		Log:  quiet(), Now: at.now,
	}

	if _, ok := reporter.Collect(context.Background()); ok {
		t.Fatal("the first pass only records a baseline")
	}

	gateway.set(gatewayText(160, 5, 1, 8000, 140, 20))
	hostA.set(vllmText(4, 2, 0.50, 13000, 80, 10))
	hostB.set(vllmText(6, 0, 0.70, 22000, 20, 0))
	at.at = at.at.Add(30 * time.Second)
	sample, ok := reporter.Collect(context.Background())
	if !ok {
		t.Fatal("second pass should produce a sample")
	}
	if sample.IntervalSeconds != 30 {
		t.Fatalf("interval = %v", sample.IntervalSeconds)
	}
	if strings.Join(sample.Sources, ",") != "gateway,model_host,dcgm" {
		t.Fatalf("sources = %v", sample.Sources)
	}
	if len(sample.Deployments) != 1 {
		t.Fatalf("deployments = %+v", sample.Deployments)
	}
	point := sample.Deployments[0]
	if point.DeploymentID != deploymentA {
		t.Fatalf("deployment = %s", point.DeploymentID)
	}
	if point.Requests["2xx"] != 60 || point.Requests["4xx"] != 3 || point.Requests["5xx"] != 0 {
		t.Fatalf("requests = %v", point.Requests)
	}
	// Generated tokens come from the engines (3000 + 2000), not the gateway's 3000.
	if point.OutputTokens != 5000 || point.InputTokens != 0 {
		t.Fatalf("tokens = in %v out %v", point.InputTokens, point.OutputTokens)
	}
	if point.Latency["0.5"] != 50 || point.Latency["5"] != 10 {
		t.Fatalf("latency = %v", point.Latency)
	}
	if point.TTFT["0.1"] != 40 || point.TTFT["0.5"] != 10 {
		t.Fatalf("ttft = %v", point.TTFT)
	}
	if point.Hosts != 2 || *point.Running != 10 || *point.Waiting != 2 || math.Abs(*point.KVCachePercent-60) > 1e-9 {
		t.Fatalf("gauges = hosts %d running %v waiting %v kv %v", point.Hosts, *point.Running, *point.Waiting, *point.KVCachePercent)
	}

	if len(sample.GPUs) != 2 {
		t.Fatalf("gpus = %+v", sample.GPUs)
	}
	ours, theirs := sample.GPUs[0], sample.GPUs[1]
	if ours.Node != "gpu-node-1" || ours.Index != "0" || ours.Product != "NVIDIA A10" ||
		ours.UtilizationPercent != 87 || ours.MemoryUsedBytes != 20000*mebibyte ||
		ours.MemoryTotalBytes != 23028*mebibyte || ours.DeploymentID != deploymentA {
		t.Fatalf("our gpu = %+v", ours)
	}
	// Held by a pod in another namespace: reported for the stamp, never for a deployment.
	if theirs.DeploymentID != "" {
		t.Fatalf("their gpu was attributed: %+v", theirs)
	}
	if point.GPUs != 1 || *point.GPUUtilizationPercent != 87 || *point.GPUMemoryTotalBytes != 23028*mebibyte {
		t.Fatalf("deployment gpu = %d %v %v", point.GPUs, *point.GPUUtilizationPercent, *point.GPUMemoryTotalBytes)
	}

	encoded, _ := json.Marshal(sample)
	if strings.Contains(string(encoded), "acct") || strings.Contains(string(encoded), "outputFromEngine") {
		t.Fatalf("sample leaks labels: %s", encoded)
	}
}

func TestUnavailableSourcesAreSkipped(t *testing.T) {
	gateway := &text{body: gatewayText(1, 0, 0, 10, 1, 0)}
	gatewayServer, _, _ := serve(t, gateway)
	host := &text{down: true}
	_, ip, port := serve(t, host)
	kube := &fakeKube{pods: `{"items":[` + podJSON("host-b-1", deploymentB, "node", ip, port) + `]}`}
	at := &clock{at: time.Unix(1_800_000_000, 0)}
	reporter := &Reporter{
		Kube: kube, Namespace: "fabric-stamp", GatewayURL: gatewayServer.URL,
		DCGM: []DCGMService{{Namespace: "missing", Name: "dcgm"}}, Log: quiet(), Now: at.now,
	}
	reporter.Collect(context.Background())
	at.at = at.at.Add(30 * time.Second)
	sample, ok := reporter.Collect(context.Background())
	if !ok || strings.Join(sample.Sources, ",") != "gateway" {
		t.Fatalf("sources = %v", sample.Sources)
	}
	// Only the gateway's idle deployment; the loading host contributes nothing, not zeros.
	for _, point := range sample.Deployments {
		if point.DeploymentID == deploymentB {
			t.Fatalf("an unreachable host produced a point: %+v", point)
		}
		if point.Running != nil || point.KVCachePercent != nil {
			t.Fatalf("gauges without a host: %+v", point)
		}
	}

	// Nothing at all answering still yields a (empty) sample rather than an error.
	gatewayServer.Close()
	kube.fail = true
	at.at = at.at.Add(30 * time.Second)
	sample, ok = reporter.Collect(context.Background())
	if !ok || len(sample.Sources) != 0 || len(sample.Deployments) != 0 || len(sample.GPUs) != 0 {
		t.Fatalf("empty sample = %+v", sample)
	}
}

func TestWithoutTheGatewayTheEngineCountsRequestsAndLatency(t *testing.T) {
	body := func(success, e2eFast float64) string {
		return fmt.Sprintf(`vllm:request_success_total{finished_reason="stop",model_name="m"} %g
vllm:prompt_tokens_total{model_name="m"} %g
vllm:e2e_request_latency_seconds_bucket{le="1.0",model_name="m"} %g
vllm:e2e_request_latency_seconds_bucket{le="+Inf",model_name="m"} %g
vllm:gpu_cache_usage_perc{model_name="m"} 0.25
`, success, success*10, e2eFast, e2eFast)
	}
	host := &text{body: body(5, 5)}
	_, ip, port := serve(t, host)
	kube := &fakeKube{pods: `{"items":[` + podJSON("host-b-1", deploymentB, "node", ip, port) + `]}`}
	at := &clock{at: time.Unix(1_800_000_000, 0)}
	reporter := &Reporter{Kube: kube, Namespace: "ns", GatewayURL: "-", Log: quiet(), Now: at.now}
	reporter.Collect(context.Background())
	host.set(body(12, 12))
	at.at = at.at.Add(15 * time.Second)
	sample, _ := reporter.Collect(context.Background())
	point := sample.Deployments[0]
	if point.Requests["2xx"] != 7 || point.InputTokens != 70 || point.Latency["1"] != 7 {
		t.Fatalf("engine fallback = %+v", point)
	}
	if *point.KVCachePercent != 25 {
		t.Fatalf("legacy KV metric = %v", *point.KVCachePercent)
	}
}

func TestANewHostOnlyBaselinesItsCounters(t *testing.T) {
	host := &text{body: vllmText(1, 0, 0.1, 50000, 100, 0)}
	_, ip, port := serve(t, host)
	kube := &fakeKube{pods: `{"items":[]}`}
	at := &clock{at: time.Unix(1_800_000_000, 0)}
	reporter := &Reporter{Kube: kube, Namespace: "ns", GatewayURL: "-", Log: quiet(), Now: at.now}
	reporter.Collect(context.Background())

	// The host appears after the baseline: its lifetime totals are not this interval's.
	kube.pods = `{"items":[` + podJSON("host-b-1", deploymentB, "node", ip, port) + `]}`
	at.at = at.at.Add(30 * time.Second)
	sample, _ := reporter.Collect(context.Background())
	point := sample.Deployments[0]
	if point.OutputTokens != 0 || point.TTFT != nil {
		t.Fatalf("a new host's history was counted: %+v", point)
	}
	if *point.Running != 1 {
		t.Fatalf("gauges should be reported at once: %+v", point)
	}

	host.set(vllmText(1, 0, 0.1, 50600, 130, 0))
	at.at = at.at.Add(30 * time.Second)
	sample, _ = reporter.Collect(context.Background())
	if sample.Deployments[0].OutputTokens != 600 || sample.Deployments[0].TTFT["0.1"] != 30 {
		t.Fatalf("second interval = %+v", sample.Deployments[0])
	}
}

func TestStepKeepsUnsentSamplesBoundedAndDropsRejectedOnes(t *testing.T) {
	gateway := &text{body: gatewayText(1, 0, 0, 1, 1, 0)}
	server, _, _ := serve(t, gateway)
	at := &clock{at: time.Unix(1_800_000_000, 0)}
	reporter := &Reporter{GatewayURL: server.URL, Log: quiet(), Now: at.now}

	var sizes []int
	result := errors.New("connection refused")
	send := func(_ context.Context, report Report) error {
		sizes = append(sizes, len(report.Samples))
		return result
	}
	for i := 0; i < MaxPending+5; i++ {
		_ = reporter.Step(context.Background(), send)
		at.at = at.at.Add(30 * time.Second)
	}
	// The first step only baselines and sends nothing.
	if len(sizes) != MaxPending+4 || sizes[0] != 1 || sizes[len(sizes)-1] != MaxPending {
		t.Fatalf("sizes = %v", sizes)
	}

	result = fmt.Errorf("%w: 422", ErrRejected)
	_ = reporter.Step(context.Background(), send)
	result = nil
	at.at = at.at.Add(30 * time.Second)
	_ = reporter.Step(context.Background(), send)
	if sizes[len(sizes)-1] != 1 {
		t.Fatalf("a rejected batch was resent: %v", sizes)
	}
}

func TestRunBacksOffWhenTheControlPlaneHasNoEndpoint(t *testing.T) {
	gateway := &text{body: gatewayText(1, 0, 0, 1, 1, 0)}
	server, _, _ := serve(t, gateway)
	reporter := &Reporter{GatewayURL: server.URL, Log: quiet(), Interval: MinInterval}
	calls := make(chan struct{}, 4)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan struct{})
	go func() {
		reporter.Run(ctx, func(context.Context, Report) error {
			calls <- struct{}{}
			return ErrNotAccepted
		})
		close(done)
	}()
	// Baseline immediately, first send after one interval, then a five-minute wait.
	select {
	case <-calls:
	case <-time.After(MinInterval + 5*time.Second):
		t.Fatal("no report was sent")
	}
	select {
	case <-calls:
		t.Fatal("retried before the back-off")
	case <-time.After(500 * time.Millisecond):
	}
	cancel()
	<-done
}
