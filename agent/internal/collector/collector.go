// Package collector moves usage from a stamp's data plane to the control plane.
//
// The data plane buffers usage locally and never sends it: it holds no telemetry
// credential, so the inference path carries no export credential. The collector is
// the only component with that credential and it is write-only, unable to read
// desired state, write status, or invoke inference.
//
// Draining is destructive, so a forward that fails would lose records if the
// collector simply dropped them. Drained records are held in a bounded pending
// queue and retried on the next pass. The queue is bounded because an outage must
// not grow memory without limit; when it overflows the oldest records are dropped
// and counted, so loss is visible rather than silent.
package collector

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"time"

	"github.com/khushwant04/fabric/agent/internal/controlplane"
)

// DrainedRecord is one record as the data plane's administrative drain returns it.
//
// AccountID and Streamed are present locally but are not forwarded: the control
// plane resolves ownership itself and refuses a record that carries it.
type DrainedRecord struct {
	RecordID     string `json:"record_id"`
	AccountID    string `json:"account_id"`
	DeploymentID string `json:"deployment_id"`
	InputTokens  int    `json:"input_tokens"`
	OutputTokens int    `json:"output_tokens"`
	Streamed     bool   `json:"streamed"`
	OccurredAt   string `json:"occurred_at"`
}

// forwardable maps a local record onto the ingestion contract.
//
// The identifier the data plane assigned at record time becomes the deduplication
// key, which is what makes a retried forward idempotent instead of double counted.
func (r DrainedRecord) forwardable() controlplane.UsageRecord {
	return controlplane.UsageRecord{
		DeploymentID:     r.DeploymentID,
		InputTokens:      r.InputTokens,
		OutputTokens:     r.OutputTokens,
		OccurredAt:       r.OccurredAt,
		DeduplicationKey: r.RecordID,
	}
}

// DataPlane drains usage from a data plane's administrative listener.
type DataPlane struct {
	BaseURL string
	HTTP    *http.Client
}

// NewDataPlane builds a client for the administrative listener.
func NewDataPlane(baseURL string, timeout time.Duration) *DataPlane {
	return &DataPlane{BaseURL: baseURL, HTTP: &http.Client{Timeout: timeout}}
}

// Drain removes and returns the data plane's buffered records.
func (d *DataPlane) Drain(ctx context.Context) ([]DrainedRecord, error) {
	request, err := http.NewRequestWithContext(
		ctx, http.MethodPost, d.BaseURL+"/admin/usage/drain", nil,
	)
	if err != nil {
		return nil, fmt.Errorf("build drain request: %w", err)
	}

	response, err := d.HTTP.Do(request)
	if err != nil {
		return nil, fmt.Errorf("drain data plane: %w", err)
	}
	defer response.Body.Close()

	payload, err := io.ReadAll(io.LimitReader(response.Body, 32<<20))
	if err != nil {
		return nil, fmt.Errorf("read drain response: %w", err)
	}
	if response.StatusCode >= 300 {
		return nil, fmt.Errorf("drain returned %d: %s", response.StatusCode, string(payload))
	}

	var body struct {
		Records []DrainedRecord `json:"records"`
		Count   int             `json:"count"`
	}
	if err := json.Unmarshal(payload, &body); err != nil {
		return nil, fmt.Errorf("decode drain response: %w", err)
	}
	return body.Records, nil
}

// Logger is the subset of logging the collector needs.
type Logger interface {
	Printf(format string, args ...any)
}

// Collector drains a data plane and forwards usage to the control plane.
type Collector struct {
	Control  *controlplane.Client
	Source   *DataPlane
	Log      Logger
	Capacity int

	pending []DrainedRecord
	dropped int
}

// Stats reports what one pass did.
type Stats struct {
	Drained    int
	Accepted   int
	Duplicates int
	Rejected   int
	Pending    int
	Dropped    int
}

// New builds a collector with a bounded pending queue.
func New(control *controlplane.Client, source *DataPlane, log Logger, capacity int) *Collector {
	if capacity <= 0 {
		capacity = 10000
	}
	return &Collector{Control: control, Source: source, Log: log, Capacity: capacity}
}

func (c *Collector) logf(format string, args ...any) {
	if c.Log != nil {
		c.Log.Printf(format, args...)
	}
}

// enqueue adds drained records, dropping the oldest if the queue is full.
func (c *Collector) enqueue(records []DrainedRecord) {
	c.pending = append(c.pending, records...)
	if overflow := len(c.pending) - c.Capacity; overflow > 0 {
		// The oldest are dropped: recent usage is the more useful signal, and an
		// unbounded queue would trade data loss for an out-of-memory kill.
		c.pending = c.pending[overflow:]
		c.dropped += overflow
		c.logf("usage queue full: dropped %d oldest records (%d total dropped)", overflow, c.dropped)
	}
}

// RunOnce drains the data plane once and forwards everything pending.
//
// A drain failure is not fatal: previously pending records are still forwarded, so
// a data plane restart does not strand a backlog.
func (c *Collector) RunOnce(ctx context.Context) (Stats, error) {
	stats := Stats{}

	drained, err := c.Source.Drain(ctx)
	if err != nil {
		c.logf("drain failed, forwarding %d pending records anyway: %v", len(c.pending), err)
	} else {
		stats.Drained = len(drained)
		c.enqueue(drained)
	}

	for len(c.pending) > 0 {
		batch := c.pending
		if len(batch) > controlplane.MaxUsageBatch {
			batch = batch[:controlplane.MaxUsageBatch]
		}

		forwardable := make([]controlplane.UsageRecord, 0, len(batch))
		for _, record := range batch {
			forwardable = append(forwardable, record.forwardable())
		}

		result, err := c.Control.ReportUsage(ctx, forwardable)
		if err != nil {
			var apiErr *controlplane.APIError
			if errors.As(err, &apiErr) && !apiErr.Retryable() {
				// A revoked credential or stamp never becomes valid again, so the
				// backlog is unforwardable and retrying only adds load.
				stats.Pending, stats.Dropped = len(c.pending), c.dropped
				return stats, fmt.Errorf("usage rejected permanently: %w", err)
			}
			// Records stay pending: the drain already removed them from the data
			// plane, so dropping them here would lose them outright.
			c.logf("forward failed, %d records still pending: %v", len(c.pending), err)
			stats.Pending, stats.Dropped = len(c.pending), c.dropped
			return stats, nil
		}

		stats.Accepted += result.Accepted
		stats.Duplicates += result.Duplicates
		stats.Rejected += result.Rejected
		for _, rejection := range result.Rejections {
			// Per-record rejections are permanent: the deployment is not placed on
			// this stamp, or the record is outside the accepted window. Retrying
			// cannot change either, so they are reported and discarded.
			c.logf("usage record rejected: code=%s deployment=%s", rejection.Code, rejection.DeploymentID)
		}

		// The whole batch is resolved once the server answers: accepted, already
		// known, or permanently refused.
		c.pending = c.pending[len(batch):]
	}

	stats.Pending, stats.Dropped = len(c.pending), c.dropped
	return stats, nil
}

// MetricsEndpoint is the model host's metrics URL, and MetricsInterval how often to
// sample it. An empty endpoint disables metrics entirely, which is the case on a stamp
// whose host is operated elsewhere and does not expose one.
type MetricsOptions struct {
	Endpoint string
	Interval time.Duration
}

// Run forwards usage on an interval until the context ends or a permanent
// rejection makes further attempts pointless.
func (c *Collector) Run(ctx context.Context, interval time.Duration) error {
	return c.RunWithMetrics(ctx, interval, MetricsOptions{})
}

// RunWithMetrics also samples GPU and runtime metrics on their own interval.
//
// Two intervals rather than one: usage should be forwarded promptly because it is
// billing-relevant, while metrics are a sampled signal whose frequency is a cost
// decision, and tying them together would force one to follow the other.
func (c *Collector) RunWithMetrics(
	ctx context.Context, interval time.Duration, metrics MetricsOptions,
) error {
	if metrics.Endpoint != "" && metrics.Interval > 0 {
		go c.sampleMetrics(ctx, metrics)
	}
	return c.runUsage(ctx, interval)
}

// sampleMetrics reports metrics until the context ends. Failures never stop it: a stamp
// that cannot report metrics should still report usage.
func (c *Collector) sampleMetrics(ctx context.Context, metrics MetricsOptions) {
	ticker := time.NewTicker(metrics.Interval)
	defer ticker.Stop()

	for {
		if err := c.ForwardMetrics(ctx, metrics.Endpoint); err != nil {
			c.logf("metrics not reported: %v", err)
		}
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		}
	}
}

func (c *Collector) runUsage(ctx context.Context, interval time.Duration) error {
	ticker := time.NewTicker(interval)
	defer ticker.Stop()

	for {
		stats, err := c.RunOnce(ctx)
		if err != nil {
			return err
		}
		if stats.Drained > 0 || stats.Accepted > 0 || stats.Pending > 0 {
			c.logf(
				"usage pass: drained=%d accepted=%d duplicates=%d rejected=%d pending=%d dropped=%d",
				stats.Drained, stats.Accepted, stats.Duplicates,
				stats.Rejected, stats.Pending, stats.Dropped,
			)
		}

		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-ticker.C:
		}
	}
}
