package collector

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func TestOnlyAllowlistedRuntimeMetricsAreKept(t *testing.T) {
	// A full scrape would carry hundreds of series that nothing consumes and that change
	// between versions.
	exposition := `# HELP vllm:num_requests_running Running requests
vllm:num_requests_running{model="launch"} 3
vllm:num_requests_waiting{model="launch"} 1
vllm:gpu_cache_usage_perc{model="launch"} 0.42
python_gc_objects_collected_total{generation="0"} 1234
process_cpu_seconds_total 8.5
`
	values := parsePrometheus(strings.NewReader(exposition), runtimeMetricNames)

	if values["vllm:num_requests_running"] != 3 {
		t.Fatalf("running requests = %v", values["vllm:num_requests_running"])
	}
	if values["vllm:gpu_cache_usage_perc"] != 0.42 {
		t.Fatalf("cache usage = %v", values["vllm:gpu_cache_usage_perc"])
	}
	if _, present := values["process_cpu_seconds_total"]; present {
		t.Fatal("an unrelated series was collected")
	}
}

func TestSeriesWithDifferentLabelsAreSummed(t *testing.T) {
	exposition := `vllm:generation_tokens_total{model="a"} 10
vllm:generation_tokens_total{model="b"} 5
`
	values := parsePrometheus(strings.NewReader(exposition), runtimeMetricNames)

	// A stamp serves one model, and a dimensional store is what per-label series would
	// need; summing is the meaningful aggregate here.
	if values["vllm:generation_tokens_total"] != 15 {
		t.Fatalf("expected 15, got %v", values["vllm:generation_tokens_total"])
	}
}

func TestAnUnreachableRuntimeIsRecordedNotFatal(t *testing.T) {
	// A host still loading weights is not yet serving metrics, which is expected.
	collector, _ := stubs(t, nil, nil)

	sample := collector.ScrapeRuntime(context.Background(), "http://127.0.0.1:1/metrics")

	if sample.Reachable {
		t.Fatal("an unreachable host was reported as reachable")
	}
	if sample.UnreachableCause == "" {
		t.Fatal("no cause was recorded, so an operator cannot tell why")
	}
}

func TestMetricsAreForwardedToTheControlPlane(t *testing.T) {
	host := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte("vllm:num_requests_running 2\nvllm:num_requests_waiting 0\n"))
	}))
	t.Cleanup(host.Close)

	var received MetricsReport
	control := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/telemetry/metrics" {
			w.WriteHeader(http.StatusNotFound)
			return
		}
		if r.Header.Get("Authorization") != "Bearer "+telemetryCredential {
			t.Errorf("metrics were sent without the telemetry credential")
		}
		_ = json.NewDecoder(r.Body).Decode(&received)
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{
			"stamp_id": "s", "gpus_recorded": 0, "runtime_recorded": true,
		})
	}))
	t.Cleanup(control.Close)

	collector, _ := stubs(t, nil, nil)
	collector.Control.BaseURL = control.URL

	if err := collector.ForwardMetrics(context.Background(), host.URL+"/metrics"); err != nil {
		t.Fatalf("forward: %v", err)
	}

	if !received.Runtime.Reachable {
		t.Fatalf("runtime was not reported reachable: %+v", received.Runtime)
	}
	if received.Runtime.Values["vllm:num_requests_running"] != 2 {
		t.Fatalf("unexpected values: %+v", received.Runtime.Values)
	}
	if received.CollectedAt == "" {
		t.Fatal("no collection time was recorded")
	}
}

func TestAFailedMetricsReportIsNotQueued(t *testing.T) {
	// The next sample replaces this one, so retrying would report the past as present.
	// The usage queue exists because every usage record matters; a metrics sample does not.
	control := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusInternalServerError)
		_ = json.NewEncoder(w).Encode(map[string]any{
			"error": map[string]string{"code": "unavailable", "message": "down"},
		})
	}))
	t.Cleanup(control.Close)

	collector, _ := stubs(t, nil, nil)
	collector.Control.BaseURL = control.URL

	err := collector.ForwardMetrics(context.Background(), "")
	if err == nil {
		t.Fatal("a failed report should surface as an error to the caller")
	}
	// Nothing accumulates: the pending queue belongs to usage and a metrics failure
	// must not add to it.
	stats, drainErr := collector.RunOnce(context.Background())
	if drainErr != nil {
		t.Fatalf("usage pass: %v", drainErr)
	}
	if stats.Pending != 0 {
		t.Fatalf("a metrics failure entered the usage queue: %+v", stats)
	}
}

func TestClockFractionIsARatioNotAVerdict(t *testing.T) {
	// The clock alone cannot say whether the cause is idleness, power, or heat.
	sample := GPUSample{SMClockMHz: 1980, SMClockMaxMHz: 3105}
	if got := sample.ClockFraction(); got < 0.63 || got > 0.64 {
		t.Fatalf("fraction = %v", got)
	}

	// A device that does not report a maximum yields zero rather than dividing by it.
	if (GPUSample{SMClockMHz: 1000}).ClockFraction() != 0 {
		t.Fatal("a missing maximum produced a fraction")
	}
}

func TestSamplingWithoutADriverIsNotAnError(t *testing.T) {
	// A stamp without a GPU is a normal configuration, not a failure.
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	samples, err := SampleGPUs(ctx)
	if err != nil {
		t.Fatalf("sampling reported an error: %v", err)
	}
	for _, sample := range samples {
		if sample.MemoryTotalMiB < 0 {
			t.Fatalf("implausible sample: %+v", sample)
		}
	}
}
