// Package collector moves usage from a stamp's data plane to the control plane.
//
// The data plane owns a durable bounded spool and never holds a telemetry credential.
// The collector leases one stable batch through the localhost admin listener, forwards
// it with its write-only credential, then acknowledges the lease only after the control
// plane resolves every record as accepted, duplicate, or permanently rejected.
//
// A timeout, collector restart, or lost acknowledgement leaves the lease in the data
// plane. The next pass receives the same records and record IDs; central deduplication
// makes that at-least-once replay safe without a second volatile queue here.
package collector

import (
	"bytes"
	"context"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"

	"github.com/khushwant04/fabric/agent/internal/controlplane"
)

// DrainedRecord is one record as the data plane's administrative lease returns it.
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
func (r DrainedRecord) forwardable() controlplane.UsageRecord {
	return controlplane.UsageRecord{
		DeploymentID:     r.DeploymentID,
		InputTokens:      r.InputTokens,
		OutputTokens:     r.OutputTokens,
		OccurredAt:       r.OccurredAt,
		DeduplicationKey: r.RecordID,
	}
}

// UsageLease remains in the data-plane spool until Ack succeeds.
type UsageLease struct {
	LeaseID string          `json:"lease_id"`
	Records []DrainedRecord `json:"records"`
	Count   int             `json:"count"`
}

func validUUID(value string) bool {
	if len(value) != 36 || value[8] != '-' || value[13] != '-' || value[18] != '-' || value[23] != '-' {
		return false
	}
	compact := strings.ReplaceAll(value, "-", "")
	if len(compact) != 32 {
		return false
	}
	_, err := hex.DecodeString(compact)
	return err == nil
}

func validateLease(lease UsageLease) error {
	if lease.Count != len(lease.Records) {
		return fmt.Errorf("usage lease count %d does not match %d records", lease.Count, len(lease.Records))
	}
	if lease.Count < 0 || lease.Count > controlplane.MaxUsageBatch {
		return fmt.Errorf("usage lease count %d is outside the protocol bound", lease.Count)
	}
	if lease.Count == 0 {
		if lease.LeaseID != "" {
			return errors.New("empty usage lease carries a lease_id")
		}
		return nil
	}
	if !validUUID(lease.LeaseID) {
		return fmt.Errorf("usage lease has invalid lease_id %q", lease.LeaseID)
	}
	seen := make(map[string]struct{}, lease.Count)
	for _, record := range lease.Records {
		if !validUUID(record.RecordID) {
			return fmt.Errorf("usage lease has invalid record_id %q", record.RecordID)
		}
		if _, duplicate := seen[record.RecordID]; duplicate {
			return fmt.Errorf("usage lease repeats record_id %q", record.RecordID)
		}
		seen[record.RecordID] = struct{}{}
	}
	return nil
}

// DataPlane accesses the localhost-only usage spool API.
type DataPlane struct {
	BaseURL string
	HTTP    *http.Client
}

// NewDataPlane builds a client for the administrative listener.
func NewDataPlane(baseURL string, timeout time.Duration) *DataPlane {
	return &DataPlane{BaseURL: baseURL, HTTP: &http.Client{Timeout: timeout}}
}

// supportsLease verifies the data plane before touching the historically destructive route.
// A new collector may start before the data-plane image during a rolling update; refusing to
// POST to an old `export_mode=drain` endpoint leaves its in-memory records intact.
func (d *DataPlane) supportsLease(ctx context.Context) (bool, error) {
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, d.BaseURL+"/admin/usage", nil)
	if err != nil {
		return false, fmt.Errorf("build usage state request: %w", err)
	}
	response, err := d.HTTP.Do(request)
	if err != nil {
		return false, fmt.Errorf("read data-plane usage state: %w", err)
	}
	defer response.Body.Close()
	payload, err := io.ReadAll(io.LimitReader(response.Body, 1<<20))
	if err != nil {
		return false, fmt.Errorf("read usage state response: %w", err)
	}
	if response.StatusCode >= 300 {
		return false, fmt.Errorf("usage state returned %d: %s", response.StatusCode, payload)
	}
	var state struct {
		ExportMode string `json:"export_mode"`
	}
	if err := json.Unmarshal(payload, &state); err != nil {
		return false, fmt.Errorf("decode usage state response: %w", err)
	}
	return state.ExportMode == "lease_ack", nil
}

// Lease returns the outstanding stable batch, or creates one from oldest records.
func (d *DataPlane) Lease(ctx context.Context, limit int) (UsageLease, error) {
	supported, err := d.supportsLease(ctx)
	if err != nil {
		return UsageLease{}, err
	}
	if !supported {
		return UsageLease{}, errors.New(
			"data plane does not support acknowledged usage leases; waiting for its upgrade",
		)
	}
	request, err := http.NewRequestWithContext(
		ctx, http.MethodPost,
		fmt.Sprintf("%s/admin/usage/drain?limit=%d", d.BaseURL, limit), nil,
	)
	if err != nil {
		return UsageLease{}, fmt.Errorf("build usage lease request: %w", err)
	}

	response, err := d.HTTP.Do(request)
	if err != nil {
		return UsageLease{}, fmt.Errorf("lease data-plane usage: %w", err)
	}
	defer response.Body.Close()

	payload, err := io.ReadAll(io.LimitReader(response.Body, 32<<20))
	if err != nil {
		return UsageLease{}, fmt.Errorf("read usage lease response: %w", err)
	}
	if response.StatusCode >= 300 {
		return UsageLease{}, fmt.Errorf("usage lease returned %d: %s", response.StatusCode, payload)
	}

	var lease UsageLease
	if err := json.Unmarshal(payload, &lease); err != nil {
		return UsageLease{}, fmt.Errorf("decode usage lease response: %w", err)
	}
	if err := validateLease(lease); err != nil {
		return UsageLease{}, err
	}
	return lease, nil
}

// Ack removes a lease after every central record result is permanent.
func (d *DataPlane) Ack(ctx context.Context, leaseID string, expected int) error {
	payload, err := json.Marshal(map[string]any{
		"lease_id": leaseID, "expected_count": expected,
	})
	if err != nil {
		return fmt.Errorf("encode usage acknowledgement: %w", err)
	}
	request, err := http.NewRequestWithContext(
		ctx, http.MethodPost, d.BaseURL+"/admin/usage/ack", bytes.NewReader(payload),
	)
	if err != nil {
		return fmt.Errorf("build usage acknowledgement: %w", err)
	}
	request.Header.Set("Content-Type", "application/json")

	response, err := d.HTTP.Do(request)
	if err != nil {
		return fmt.Errorf("acknowledge data-plane usage: %w", err)
	}
	defer response.Body.Close()
	body, err := io.ReadAll(io.LimitReader(response.Body, 1<<20))
	if err != nil {
		return fmt.Errorf("read usage acknowledgement: %w", err)
	}
	if response.StatusCode >= 300 {
		return fmt.Errorf("usage acknowledgement returned %d: %s", response.StatusCode, body)
	}
	var result struct {
		LeaseID      string `json:"lease_id"`
		Acknowledged int    `json:"acknowledged"`
	}
	if err := json.Unmarshal(body, &result); err != nil {
		return fmt.Errorf("decode usage acknowledgement: %w", err)
	}
	if result.LeaseID != leaseID {
		return fmt.Errorf("usage acknowledgement returned lease %q, expected %q", result.LeaseID, leaseID)
	}
	if result.Acknowledged != expected {
		return fmt.Errorf(
			"usage acknowledgement removed %d of %d leased records",
			result.Acknowledged, expected,
		)
	}
	return nil
}

// Logger is the subset of logging the collector needs.
type Logger interface {
	Printf(format string, args ...any)
}

// Collector leases usage and forwards it to the control plane.
type Collector struct {
	Control *controlplane.Client
	Source  *DataPlane
	Log     Logger
	// Capacity bounds records resolved by one pass. The spool itself owns backlog bounds.
	Capacity int
}

// Stats reports what one pass did.
type Stats struct {
	Drained      int
	Accepted     int
	Duplicates   int
	Rejected     int
	Acknowledged int
	Pending      int
	// Kept in the public shape for log compatibility. Drops now happen durably in the
	// data plane and are reported by its /admin/usage state, never in collector memory.
	Dropped int
}

// New builds a collector with bounded work per pass.
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

func validateUsageResult(result *controlplane.UsageIngestResult, count int) error {
	if result == nil {
		return errors.New("central usage response is empty")
	}
	if result.Accepted < 0 || result.Duplicates < 0 || result.Rejected < 0 {
		return errors.New("central usage response contains a negative count")
	}
	if result.Accepted+result.Duplicates+result.Rejected != count {
		return fmt.Errorf(
			"central usage response resolved %d of %d records",
			result.Accepted+result.Duplicates+result.Rejected, count,
		)
	}
	if len(result.Rejections) != result.Rejected {
		return fmt.Errorf(
			"central usage response reports %d rejections but describes %d",
			result.Rejected, len(result.Rejections),
		)
	}
	seen := make(map[int]struct{}, len(result.Rejections))
	for _, rejection := range result.Rejections {
		if rejection.Index < 0 || rejection.Index >= count {
			return fmt.Errorf("central usage rejection index %d is outside the batch", rejection.Index)
		}
		if _, duplicate := seen[rejection.Index]; duplicate {
			return fmt.Errorf("central usage rejection index %d is duplicated", rejection.Index)
		}
		if rejection.Code == "" {
			return fmt.Errorf("central usage rejection index %d has no reason code", rejection.Index)
		}
		seen[rejection.Index] = struct{}{}
	}
	return nil
}

// RunOnce resolves up to Capacity records from stable data-plane leases.
//
// Source and central failures are retryable without local state: an unacknowledged lease remains
// durable in the data plane. A permanent credential/stamp rejection stops the collector but also
// leaves the lease untouched, so rotating the credential can resume it later.
func (c *Collector) RunOnce(ctx context.Context) (Stats, error) {
	stats := Stats{}
	remaining := c.Capacity

	for remaining > 0 {
		limit := remaining
		if limit > controlplane.MaxUsageBatch {
			limit = controlplane.MaxUsageBatch
		}
		lease, err := c.Source.Lease(ctx, limit)
		if err != nil {
			c.logf("usage lease failed: %v", err)
			return stats, nil
		}
		if len(lease.Records) == 0 {
			return stats, nil
		}
		stats.Drained += len(lease.Records)

		forwardable := make([]controlplane.UsageRecord, 0, len(lease.Records))
		for _, record := range lease.Records {
			forwardable = append(forwardable, record.forwardable())
		}

		result, err := c.Control.ReportUsage(ctx, forwardable)
		if err != nil {
			stats.Pending = len(lease.Records)
			var apiErr *controlplane.APIError
			if errors.As(err, &apiErr) && !apiErr.Retryable() {
				return stats, fmt.Errorf("usage rejected permanently: %w", err)
			}
			c.logf("usage forward failed, lease %s remains durable: %v", lease.LeaseID, err)
			return stats, nil
		}

		if err := validateUsageResult(result, len(lease.Records)); err != nil {
			stats.Pending = len(lease.Records)
			c.logf("invalid central usage response, lease %s remains durable: %v", lease.LeaseID, err)
			return stats, nil
		}

		// These counts describe what central ingestion actually did, even if the local ack fails.
		// A later replay then honestly reports duplicates rather than erasing this acceptance.
		stats.Accepted += result.Accepted
		stats.Duplicates += result.Duplicates
		stats.Rejected += result.Rejected
		for _, rejection := range result.Rejections {
			// Per-record rejections are permanent: placement and timestamp validation cannot
			// change for this event, so the successful response resolves the whole lease.
			c.logf("usage record rejected: code=%s deployment=%s", rejection.Code, rejection.DeploymentID)
		}
		if err := c.Source.Ack(ctx, lease.LeaseID, len(lease.Records)); err != nil {
			// Central storage may already contain these IDs. Leave the lease and replay;
			// the next central response reports duplicates, then acknowledgement can finish.
			stats.Pending = len(lease.Records)
			c.logf(
				"central usage resolved lease %s (accepted=%d duplicates=%d rejected=%d) but acknowledgement failed; it will replay: %v",
				lease.LeaseID, result.Accepted, result.Duplicates, result.Rejected, err,
			)
			return stats, nil
		}

		stats.Acknowledged += len(lease.Records)
		remaining -= len(lease.Records)
	}
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
				"usage pass: leased=%d accepted=%d duplicates=%d rejected=%d acknowledged=%d pending=%d",
				stats.Drained, stats.Accepted, stats.Duplicates,
				stats.Rejected, stats.Acknowledged, stats.Pending,
			)
		}

		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-ticker.C:
		}
	}
}
