// Package servingmetrics pushes compact serving metrics for this stamp's deployments and GPUs
// to the control plane, so the console can chart a model serving without Grafana.
//
// The control plane never connects to a stamp (ADR 0001, ADR 0007), and Prometheus runs only
// on the central cluster, where it cannot see a stamp on a VM. So the agent reads the numbers
// where they are, next to it, and pushes small aggregates outbound like everything else it
// sends:
//
//   - the stamp gateway's own /metrics, in the same pod: requests by outcome, end-to-end
//     latency, tokens. Only the gateway sees requests refused before a model host.
//   - each model host's vLLM /metrics, by pod IP: time to first token, inter-token latency,
//     running and waiting requests, KV-cache use, generated tokens.
//   - an NVIDIA DCGM exporter, found through its Service's Endpoints: GPU utilisation and
//     memory per device.
//
// Every interval (30 s by default) it turns the counters into deltas, the histograms into
// per-bucket counts observed in the interval, and the gauges into current values, and sends
// one bounded sample. A source that cannot be read is skipped; the sample says which
// sources answered. Nothing raw leaves the stamp: no label values other than deployment
// ids, GPU indices, node names and the GPU product.
package servingmetrics

import (
	"context"
	"errors"
	"log/slog"
	"net/http"
	"time"
)

const (
	// DefaultInterval is how often a sample is taken and sent.
	DefaultInterval = 30 * time.Second
	// MinInterval keeps a misconfiguration from scraping every host every second.
	MinInterval = 10 * time.Second
	// DefaultGatewayURL is the stamp gateway's inference listener in the same pod, which is
	// where its unauthenticated Prometheus endpoint lives.
	DefaultGatewayURL = "http://127.0.0.1:8080/metrics"
	// DefaultModelHostPort is vLLM's port when the pod spec does not name one.
	DefaultModelHostPort = 8000

	// MaxDeployments, MaxGPUs and MaxPending bound one report. The control plane enforces
	// the same limits.
	MaxDeployments = 64
	MaxGPUs        = 128
	MaxPending     = 20
	// MaxHosts bounds how many model-host pods are scraped in one pass.
	MaxHosts = 64
	// MaxBuckets bounds one histogram.
	MaxBuckets = 64

	// NotAcceptedBackoff is the wait after a control plane said it has no metrics endpoint.
	NotAcceptedBackoff = 5 * time.Minute
	scrapeTimeout      = 4 * time.Second
	scrapeParallelism  = 8
)

// ErrNotAccepted is returned by a sender when the control plane does not know the endpoint.
var ErrNotAccepted = errors.New("control plane does not accept serving metrics")

// ErrRejected is returned by a sender when the control plane refused the report itself, so
// resending the same samples cannot succeed.
var ErrRejected = errors.New("control plane rejected the serving metrics report")

// Reader is the Kubernetes access this package needs, narrow so a test can fake it.
type Reader interface {
	Get(ctx context.Context, path string, out any) error
}

// DeploymentPoint is one deployment's aggregate over one interval. Counts are deltas over
// the interval; gauges are the value when the sample was taken; histograms hold the
// observations made during the interval.
type DeploymentPoint struct {
	DeploymentID string `json:"deployment_id"`
	// Requests counts finished requests by class: "2xx", "4xx" (refused or rate limited by
	// the gateway, or a client error from the host), "5xx" (host error or unreachable) and
	// "cancelled" (the caller went away).
	Requests     map[string]float64 `json:"requests,omitempty"`
	InputTokens  float64            `json:"input_tokens"`
	OutputTokens float64            `json:"output_tokens"`
	// Latency is end-to-end as the caller saw it, from the gateway; vLLM's own end-to-end
	// latency stands in when the gateway could not be read.
	Latency Histogram `json:"latency_seconds,omitempty"`
	TTFT    Histogram `json:"ttft_seconds,omitempty"`
	ITL     Histogram `json:"itl_seconds,omitempty"`
	// Hosts is how many model hosts answered; the gauges below are present only when one did.
	Hosts          int      `json:"hosts"`
	Running        *float64 `json:"running,omitempty"`
	Waiting        *float64 `json:"waiting,omitempty"`
	KVCachePercent *float64 `json:"kv_cache_percent,omitempty"`
	// The GPUs attributed to this deployment's hosts, when a GPU source answered.
	GPUs                  int      `json:"gpus"`
	GPUUtilizationPercent *float64 `json:"gpu_utilization_percent,omitempty"`
	GPUMemoryUsedBytes    *float64 `json:"gpu_memory_used_bytes,omitempty"`
	GPUMemoryTotalBytes   *float64 `json:"gpu_memory_total_bytes,omitempty"`

	// outputFromEngine records that OutputTokens now holds vLLM's generated-token count
	// rather than the gateway's.
	outputFromEngine bool
}

// GPUPoint is one device's reading.
type GPUPoint struct {
	Node               string  `json:"node"`
	Index              string  `json:"index"`
	Product            string  `json:"product,omitempty"`
	UtilizationPercent float64 `json:"utilization_percent"`
	MemoryUsedBytes    float64 `json:"memory_used_bytes"`
	MemoryTotalBytes   float64 `json:"memory_total_bytes"`
	// DeploymentID is set when the device is known to serve one deployment.
	DeploymentID string `json:"deployment_id,omitempty"`
}

// Sample is one interval.
type Sample struct {
	ObservedAt      time.Time         `json:"observed_at"`
	IntervalSeconds float64           `json:"interval_seconds"`
	Sources         []string          `json:"sources"`
	Deployments     []DeploymentPoint `json:"deployments"`
	GPUs            []GPUPoint        `json:"gpus"`
}

// Report is what is sent: the newest sample, preceded by any that failed to send.
type Report struct {
	Samples []Sample `json:"samples"`
}

// Sender delivers a report to the control plane.
type Sender func(ctx context.Context, report Report) error

// DCGMService names the Service in front of a DCGM exporter.
type DCGMService struct {
	Namespace string
	Name      string
}

// Reporter samples and sends on a fixed interval.
type Reporter struct {
	// Kube lists model-host pods and reads the DCGM Endpoints. Nil reads the gateway only.
	Kube      Reader
	Namespace string
	// HTTP scrapes the sources. Nil uses a client with a short timeout.
	HTTP       *http.Client
	GatewayURL string
	// DCGM lists the exporters to read; empty reports no GPU devices.
	DCGM     []DCGMService
	Interval time.Duration
	Log      *slog.Logger
	Now      func() time.Time

	state   *collectorState
	pending []Sample
}

func (r *Reporter) now() time.Time {
	if r.Now != nil {
		return r.Now()
	}
	return time.Now()
}

func (r *Reporter) logger() *slog.Logger {
	if r.Log != nil {
		return r.Log
	}
	return slog.Default()
}

func (r *Reporter) interval() time.Duration {
	if r.Interval <= 0 {
		return DefaultInterval
	}
	if r.Interval < MinInterval {
		return MinInterval
	}
	return r.Interval
}

// Run samples and sends until the context ends. Failures are logged when they change and
// never stop the agent: metrics are for a human watching, and serving does not depend on them.
func (r *Reporter) Run(ctx context.Context, send Sender) {
	var lastError string
	timer := time.NewTimer(0)
	defer timer.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-timer.C:
		}
		wait := r.interval()
		err := r.Step(ctx, send)
		if errors.Is(err, ErrNotAccepted) {
			wait = NotAcceptedBackoff
		}
		if err != nil && ctx.Err() == nil {
			if message := err.Error(); message != lastError {
				r.logger().Warn("serving metrics report failed", "error", err)
				lastError = message
			}
		} else if err == nil && lastError != "" {
			r.logger().Info("serving metrics reports recovered")
			lastError = ""
		}
		timer.Reset(wait)
	}
}

// Step takes one sample and sends it with any unsent ones. The first call only records the
// counters it will take deltas from, and sends nothing.
func (r *Reporter) Step(ctx context.Context, send Sender) error {
	sample, ok := r.Collect(ctx)
	if !ok {
		return nil
	}
	r.pending = append(r.pending, sample)
	if len(r.pending) > MaxPending {
		r.pending = r.pending[len(r.pending)-MaxPending:]
	}
	err := send(ctx, Report{Samples: r.pending})
	switch {
	case err == nil:
		r.pending = nil
	case errors.Is(err, ErrNotAccepted), errors.Is(err, ErrRejected):
		// Resending cannot help: an older control plane, or a report it will never accept.
		r.pending = nil
	}
	return err
}
