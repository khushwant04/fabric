package collector

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/khushwant04/fabric/agent/internal/controlplane"
)

const telemetryCredential = "fbt_telemetry_secret"

type capture struct {
	batches     [][]controlplane.UsageRecord
	authHeaders []string
	drains      int
}

func (c *capture) lastBatch() []controlplane.UsageRecord {
	if len(c.batches) == 0 {
		return nil
	}
	return c.batches[len(c.batches)-1]
}

// stubs builds a data plane and a control plane, both real HTTP servers.
func stubs(t *testing.T, records []DrainedRecord, ingest func(int) (int, any)) (*Collector, *capture) {
	t.Helper()
	seen := &capture{}

	dataPlane := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost || r.URL.Path != "/admin/usage/drain" {
			w.WriteHeader(http.StatusNotFound)
			return
		}
		seen.drains++
		batch := records
		if seen.drains > 1 {
			// A drain is destructive: the second call returns nothing.
			batch = nil
		}
		_ = json.NewEncoder(w).Encode(map[string]any{"records": batch, "count": len(batch)})
	}))
	t.Cleanup(dataPlane.Close)

	control := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		seen.authHeaders = append(seen.authHeaders, r.Header.Get("Authorization"))
		var body struct {
			Records []controlplane.UsageRecord `json:"records"`
		}
		_ = json.NewDecoder(r.Body).Decode(&body)
		seen.batches = append(seen.batches, body.Records)

		status, payload := http.StatusOK, any(map[string]any{
			"accepted": len(body.Records), "duplicates": 0, "rejected": 0,
			"rejections": []any{},
		})
		if ingest != nil {
			status, payload = ingest(len(seen.batches))
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(status)
		_ = json.NewEncoder(w).Encode(payload)
	}))
	t.Cleanup(control.Close)

	client := controlplane.New(control.URL, 5*time.Second)
	client.Credential = telemetryCredential

	return New(client, NewDataPlane(dataPlane.URL, 5*time.Second), nil, 0), seen
}

func record(id, deployment string, in, out int) DrainedRecord {
	return DrainedRecord{
		RecordID:     id,
		AccountID:    "11111111-1111-1111-1111-111111111111",
		DeploymentID: deployment,
		InputTokens:  in,
		OutputTokens: out,
		Streamed:     false,
		OccurredAt:   time.Now().UTC().Format(time.RFC3339),
	}
}

func TestForwardsDrainedRecordsWithTheTelemetryCredential(t *testing.T) {
	drained := []DrainedRecord{
		record("aaaaaaaa-0000-0000-0000-000000000001", "dep-1", 10, 5),
		record("aaaaaaaa-0000-0000-0000-000000000002", "dep-1", 7, 3),
	}
	collector, seen := stubs(t, drained, nil)

	stats, err := collector.RunOnce(context.Background())
	if err != nil {
		t.Fatalf("RunOnce: %v", err)
	}
	if stats.Drained != 2 || stats.Accepted != 2 || stats.Pending != 0 {
		t.Fatalf("unexpected stats: %+v", stats)
	}
	if got := seen.authHeaders[0]; got != "Bearer "+telemetryCredential {
		t.Fatalf("wrong credential presented: %q", got)
	}
}

func TestOwnershipFieldsAreNotForwarded(t *testing.T) {
	// The server refuses unknown fields, and ownership must be resolved centrally,
	// so the collector must strip what it holds locally rather than pass it on.
	collector, seen := stubs(t, []DrainedRecord{
		record("aaaaaaaa-0000-0000-0000-000000000001", "dep-1", 10, 5),
	}, nil)

	if _, err := collector.RunOnce(context.Background()); err != nil {
		t.Fatalf("RunOnce: %v", err)
	}

	encoded, err := json.Marshal(seen.lastBatch()[0])
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	var fields map[string]any
	if err := json.Unmarshal(encoded, &fields); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	for _, forbidden := range []string{"account_id", "stamp_id", "streamed", "record_id"} {
		if _, present := fields[forbidden]; present {
			t.Fatalf("forwarded %q, which ingestion forbids: %s", forbidden, encoded)
		}
	}
	if fields["deduplication_key"] != "aaaaaaaa-0000-0000-0000-000000000001" {
		t.Fatalf("the record identifier must become the deduplication key: %s", encoded)
	}
}

func TestAFailedForwardKeepsRecordsForTheNextPass(t *testing.T) {
	// The drain already removed these from the data plane, so dropping them here
	// would lose them outright.
	drained := []DrainedRecord{record("aaaaaaaa-0000-0000-0000-000000000001", "dep-1", 10, 5)}
	collector, seen := stubs(t, drained, func(attempt int) (int, any) {
		if attempt == 1 {
			return http.StatusServiceUnavailable, map[string]any{
				"error": map[string]any{"code": "unavailable", "message": "try later"},
			}
		}
		return http.StatusOK, map[string]any{
			"accepted": 1, "duplicates": 0, "rejected": 0, "rejections": []any{},
		}
	})

	first, err := collector.RunOnce(context.Background())
	if err != nil {
		t.Fatalf("a retryable failure must not end the pass: %v", err)
	}
	if first.Pending != 1 || first.Accepted != 0 {
		t.Fatalf("records were not retained: %+v", first)
	}

	second, err := collector.RunOnce(context.Background())
	if err != nil {
		t.Fatalf("second RunOnce: %v", err)
	}
	if second.Accepted != 1 || second.Pending != 0 {
		t.Fatalf("retained records were not forwarded: %+v", second)
	}
	if seen.drains != 2 {
		t.Fatalf("expected a drain per pass, got %d", seen.drains)
	}
}

func TestAPermanentRejectionStopsTheCollector(t *testing.T) {
	drained := []DrainedRecord{record("aaaaaaaa-0000-0000-0000-000000000001", "dep-1", 10, 5)}
	collector, _ := stubs(t, drained, func(int) (int, any) {
		return http.StatusUnauthorized, map[string]any{
			"error": map[string]any{"code": "credential_revoked", "message": "revoked"},
		}
	})

	if _, err := collector.RunOnce(context.Background()); err == nil {
		t.Fatal("a revoked credential must stop the collector rather than loop forever")
	}
}

func TestRejectedRecordsAreDiscardedNotRetried(t *testing.T) {
	// An unplaced deployment or an out-of-window timestamp cannot become valid, so
	// retrying would resend the same record forever.
	drained := []DrainedRecord{record("aaaaaaaa-0000-0000-0000-000000000001", "dep-gone", 10, 5)}
	collector, seen := stubs(t, drained, func(int) (int, any) {
		return http.StatusOK, map[string]any{
			"accepted": 0, "duplicates": 0, "rejected": 1,
			"rejections": []any{map[string]any{
				"index": 0, "code": "deployment_not_placed_on_stamp", "deployment_id": "dep-gone",
			}},
		}
	})

	stats, err := collector.RunOnce(context.Background())
	if err != nil {
		t.Fatalf("RunOnce: %v", err)
	}
	if stats.Rejected != 1 || stats.Pending != 0 {
		t.Fatalf("rejected records must not stay pending: %+v", stats)
	}

	if _, err := collector.RunOnce(context.Background()); err != nil {
		t.Fatalf("second RunOnce: %v", err)
	}
	if len(seen.batches) != 1 {
		t.Fatalf("a permanently rejected record was resent: %d batches", len(seen.batches))
	}
}

func TestDuplicatesAreNotAnError(t *testing.T) {
	drained := []DrainedRecord{record("aaaaaaaa-0000-0000-0000-000000000001", "dep-1", 10, 5)}
	collector, _ := stubs(t, drained, func(int) (int, any) {
		return http.StatusOK, map[string]any{
			"accepted": 0, "duplicates": 1, "rejected": 0, "rejections": []any{},
		}
	})

	stats, err := collector.RunOnce(context.Background())
	if err != nil {
		t.Fatalf("a duplicate is the expected outcome of an at-least-once retry: %v", err)
	}
	if stats.Duplicates != 1 || stats.Pending != 0 {
		t.Fatalf("unexpected stats: %+v", stats)
	}
}

func TestNothingIsSentWhenThereIsNothingToReport(t *testing.T) {
	collector, seen := stubs(t, nil, nil)

	stats, err := collector.RunOnce(context.Background())
	if err != nil {
		t.Fatalf("RunOnce: %v", err)
	}
	if stats.Drained != 0 {
		t.Fatalf("unexpected stats: %+v", stats)
	}
	if len(seen.batches) != 0 {
		t.Fatal("an empty buffer must not produce a control-plane request")
	}
}

func TestABacklogIsChunkedToTheServerLimit(t *testing.T) {
	drained := make([]DrainedRecord, controlplane.MaxUsageBatch+25)
	for i := range drained {
		drained[i] = record(fmt.Sprintf("aaaaaaaa-0000-0000-0000-%012d", i), "dep-1", 1, 1)
	}
	collector, seen := stubs(t, drained, nil)

	stats, err := collector.RunOnce(context.Background())
	if err != nil {
		t.Fatalf("RunOnce: %v", err)
	}
	if len(seen.batches) != 2 {
		t.Fatalf("expected two batches, got %d", len(seen.batches))
	}
	if len(seen.batches[0]) != controlplane.MaxUsageBatch || len(seen.batches[1]) != 25 {
		t.Fatalf("wrong chunk sizes: %d, %d", len(seen.batches[0]), len(seen.batches[1]))
	}
	if stats.Accepted != len(drained) {
		t.Fatalf("not every record was forwarded: %+v", stats)
	}
}

func TestTheQueueIsBoundedAndDropsAreCounted(t *testing.T) {
	// An outage must not grow memory without limit, and the loss must be visible.
	drained := []DrainedRecord{
		record("aaaaaaaa-0000-0000-0000-000000000001", "dep-1", 1, 1),
		record("aaaaaaaa-0000-0000-0000-000000000002", "dep-1", 2, 2),
		record("aaaaaaaa-0000-0000-0000-000000000003", "dep-1", 3, 3),
	}
	collector, seen := stubs(t, drained, func(attempt int) (int, any) {
		if attempt == 1 {
			return http.StatusServiceUnavailable, map[string]any{
				"error": map[string]any{"code": "unavailable", "message": "down"},
			}
		}
		return http.StatusOK, map[string]any{
			"accepted": 2, "duplicates": 0, "rejected": 0, "rejections": []any{},
		}
	})
	collector.Capacity = 2

	first, err := collector.RunOnce(context.Background())
	if err != nil {
		t.Fatalf("RunOnce: %v", err)
	}
	if first.Pending != 2 || first.Dropped != 1 {
		t.Fatalf("queue was not bounded: %+v", first)
	}

	if _, err := collector.RunOnce(context.Background()); err != nil {
		t.Fatalf("second RunOnce: %v", err)
	}
	// The oldest was dropped, so the newest two survive.
	kept := seen.batches[len(seen.batches)-1]
	if len(kept) != 2 || kept[0].InputTokens != 2 || kept[1].InputTokens != 3 {
		t.Fatalf("the wrong records were kept: %+v", kept)
	}
}

func TestADrainFailureStillForwardsPendingRecords(t *testing.T) {
	// A data plane restart must not strand a backlog the collector already holds.
	drained := []DrainedRecord{record("aaaaaaaa-0000-0000-0000-000000000001", "dep-1", 10, 5)}
	collector, seen := stubs(t, drained, func(attempt int) (int, any) {
		if attempt == 1 {
			return http.StatusServiceUnavailable, map[string]any{
				"error": map[string]any{"code": "unavailable", "message": "down"},
			}
		}
		return http.StatusOK, map[string]any{
			"accepted": 1, "duplicates": 0, "rejected": 0, "rejections": []any{},
		}
	})

	if _, err := collector.RunOnce(context.Background()); err != nil {
		t.Fatalf("first RunOnce: %v", err)
	}

	// Point the collector at a data plane that is gone.
	collector.Source = NewDataPlane("http://127.0.0.1:1", 200*time.Millisecond)

	stats, err := collector.RunOnce(context.Background())
	if err != nil {
		t.Fatalf("a drain failure must not end the pass: %v", err)
	}
	if stats.Accepted != 1 || stats.Pending != 0 {
		t.Fatalf("the pending backlog was not forwarded: %+v", stats)
	}
	if len(seen.batches) != 2 {
		t.Fatalf("expected a second forward attempt, got %d", len(seen.batches))
	}
}
