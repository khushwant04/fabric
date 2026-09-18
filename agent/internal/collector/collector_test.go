package collector

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strconv"
	"testing"
	"time"

	"github.com/khushwant04/fabric/agent/internal/controlplane"
)

const (
	telemetryCredential = "fbt_telemetry_secret"
	leaseID             = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
)

type capture struct {
	batches      [][]controlplane.UsageRecord
	authHeaders  []string
	leases       int
	acks         int
	ackFailures  int
	malformedAck bool
	records      []DrainedRecord
	leased       []DrainedRecord
	dataPlaneURL string
	controlURL   string
}

func (c *capture) lastBatch() []controlplane.UsageRecord {
	if len(c.batches) == 0 {
		return nil
	}
	return c.batches[len(c.batches)-1]
}

// stubs builds acknowledged data-plane spool and control-plane ingestion servers.
func stubs(t *testing.T, records []DrainedRecord, ingest func(int) (int, any)) (*Collector, *capture) {
	t.Helper()
	seen := &capture{records: append([]DrainedRecord(nil), records...)}

	dataPlane := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.Method == http.MethodGet && r.URL.Path == "/admin/usage":
			_ = json.NewEncoder(w).Encode(map[string]any{"export_mode": "lease_ack"})
		case r.Method == http.MethodPost && r.URL.Path == "/admin/usage/drain":
			seen.leases++
			if len(seen.leased) == 0 && len(seen.records) > 0 {
				limit, _ := strconv.Atoi(r.URL.Query().Get("limit"))
				if limit <= 0 || limit > len(seen.records) {
					limit = len(seen.records)
				}
				seen.leased = append([]DrainedRecord(nil), seen.records[:limit]...)
			}
			id := ""
			if len(seen.leased) > 0 {
				id = leaseID
			}
			_ = json.NewEncoder(w).Encode(map[string]any{
				"lease_id": id, "records": seen.leased, "count": len(seen.leased),
			})
		case r.Method == http.MethodPost && r.URL.Path == "/admin/usage/ack":
			if seen.malformedAck {
				_ = json.NewEncoder(w).Encode(map[string]any{})
				return
			}
			if seen.ackFailures > 0 {
				seen.ackFailures--
				http.Error(w, "temporary ack failure", http.StatusServiceUnavailable)
				return
			}
			var body struct {
				LeaseID       string `json:"lease_id"`
				ExpectedCount int    `json:"expected_count"`
			}
			_ = json.NewDecoder(r.Body).Decode(&body)
			if body.LeaseID != leaseID || body.ExpectedCount != len(seen.leased) {
				http.Error(w, "wrong lease or count", http.StatusConflict)
				return
			}
			acknowledged := len(seen.leased)
			seen.acks++
			_ = json.NewEncoder(w).Encode(map[string]any{
				"lease_id": leaseID, "acknowledged": acknowledged,
			})
			seen.records = seen.records[acknowledged:]
			seen.leased = nil
		default:
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	t.Cleanup(dataPlane.Close)
	seen.dataPlaneURL = dataPlane.URL

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
	seen.controlURL = control.URL

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

func TestForwardsAndAcknowledgesLeasedRecords(t *testing.T) {
	records := []DrainedRecord{
		record("aaaaaaaa-0000-0000-0000-000000000001", "dep-1", 10, 5),
		record("aaaaaaaa-0000-0000-0000-000000000002", "dep-1", 7, 3),
	}
	worker, seen := stubs(t, records, nil)

	stats, err := worker.RunOnce(context.Background())
	if err != nil {
		t.Fatalf("RunOnce: %v", err)
	}
	if stats.Drained != 2 || stats.Accepted != 2 || stats.Pending != 0 {
		t.Fatalf("unexpected stats: %+v", stats)
	}
	if seen.acks != 1 || len(seen.records) != 0 {
		t.Fatalf("resolved lease was not acknowledged: acks=%d records=%d", seen.acks, len(seen.records))
	}
	if got := seen.authHeaders[0]; got != "Bearer "+telemetryCredential {
		t.Fatalf("wrong credential presented: %q", got)
	}
}

func TestOwnershipFieldsAreNotForwarded(t *testing.T) {
	worker, seen := stubs(t, []DrainedRecord{
		record("aaaaaaaa-0000-0000-0000-000000000001", "dep-1", 10, 5),
	}, nil)
	if _, err := worker.RunOnce(context.Background()); err != nil {
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
		t.Fatalf("record identifier must become the deduplication key: %s", encoded)
	}
}

func TestRetryableForwardFailureLeavesLeaseInDataPlane(t *testing.T) {
	worker, seen := stubs(t, []DrainedRecord{
		record("aaaaaaaa-0000-0000-0000-000000000001", "dep-1", 10, 5),
	}, func(attempt int) (int, any) {
		if attempt == 1 {
			return http.StatusServiceUnavailable, map[string]any{
				"error": map[string]any{"code": "unavailable", "message": "try later"},
			}
		}
		return http.StatusOK, map[string]any{
			"accepted": 1, "duplicates": 0, "rejected": 0, "rejections": []any{},
		}
	})

	first, err := worker.RunOnce(context.Background())
	if err != nil || first.Pending != 1 || seen.acks != 0 {
		t.Fatalf("lease must remain after retryable failure: stats=%+v err=%v", first, err)
	}
	second, err := worker.RunOnce(context.Background())
	if err != nil || second.Accepted != 1 || second.Pending != 0 || seen.acks != 1 {
		t.Fatalf("stable lease was not retried and acknowledged: stats=%+v err=%v", second, err)
	}
	if seen.leases < 2 || seen.batches[0][0].DeduplicationKey != seen.batches[1][0].DeduplicationKey {
		t.Fatalf("retry did not preserve the leased record identity: leases=%d batches=%+v", seen.leases, seen.batches)
	}
}

func TestCollectorRestartNeedsNoLocalPendingState(t *testing.T) {
	worker, seen := stubs(t, []DrainedRecord{
		record("aaaaaaaa-0000-0000-0000-000000000001", "dep-1", 10, 5),
	}, func(attempt int) (int, any) {
		if attempt == 1 {
			return http.StatusServiceUnavailable, map[string]any{
				"error": map[string]any{"code": "unavailable", "message": "down"},
			}
		}
		return http.StatusOK, map[string]any{
			"accepted": 1, "duplicates": 0, "rejected": 0, "rejections": []any{},
		}
	})
	if _, err := worker.RunOnce(context.Background()); err != nil {
		t.Fatalf("first pass: %v", err)
	}

	client := controlplane.New(seen.controlURL, 5*time.Second)
	client.Credential = telemetryCredential
	restarted := New(client, NewDataPlane(seen.dataPlaneURL, 5*time.Second), nil, 10000)
	stats, err := restarted.RunOnce(context.Background())
	if err != nil || stats.Accepted != 1 || seen.acks != 1 {
		t.Fatalf("restarted collector did not resume durable lease: stats=%+v err=%v", stats, err)
	}
}

func TestPermanentCredentialFailureStopsWithoutAcknowledging(t *testing.T) {
	worker, seen := stubs(t, []DrainedRecord{
		record("aaaaaaaa-0000-0000-0000-000000000001", "dep-1", 10, 5),
	}, func(int) (int, any) {
		return http.StatusUnauthorized, map[string]any{
			"error": map[string]any{"code": "credential_revoked", "message": "revoked"},
		}
	})
	stats, err := worker.RunOnce(context.Background())
	if err == nil || stats.Pending != 1 || seen.acks != 0 || len(seen.records) != 1 {
		t.Fatalf("permanent auth failure must preserve durable lease: stats=%+v err=%v", stats, err)
	}
}

func TestPerRecordRejectionsResolveAndAcknowledgeTheLease(t *testing.T) {
	worker, seen := stubs(t, []DrainedRecord{
		record("aaaaaaaa-0000-0000-0000-000000000001", "dep-gone", 10, 5),
	}, func(int) (int, any) {
		return http.StatusOK, map[string]any{
			"accepted": 0, "duplicates": 0, "rejected": 1,
			"rejections": []any{map[string]any{
				"index": 0, "code": "deployment_not_placed_on_stamp", "deployment_id": "dep-gone",
			}},
		}
	})
	stats, err := worker.RunOnce(context.Background())
	if err != nil || stats.Rejected != 1 || seen.acks != 1 {
		t.Fatalf("permanent record result must acknowledge: stats=%+v err=%v", stats, err)
	}
	if _, err := worker.RunOnce(context.Background()); err != nil || len(seen.batches) != 1 {
		t.Fatalf("resolved rejection was replayed: batches=%d err=%v", len(seen.batches), err)
	}
}

func TestDuplicatesResolveAndAcknowledgeTheLease(t *testing.T) {
	worker, seen := stubs(t, []DrainedRecord{
		record("aaaaaaaa-0000-0000-0000-000000000001", "dep-1", 10, 5),
	}, func(int) (int, any) {
		return http.StatusOK, map[string]any{
			"accepted": 0, "duplicates": 1, "rejected": 0, "rejections": []any{},
		}
	})
	stats, err := worker.RunOnce(context.Background())
	if err != nil || stats.Duplicates != 1 || seen.acks != 1 {
		t.Fatalf("duplicate must resolve durable lease: stats=%+v err=%v", stats, err)
	}
}

func TestNothingIsSentWhenThereIsNoLease(t *testing.T) {
	worker, seen := stubs(t, nil, nil)
	stats, err := worker.RunOnce(context.Background())
	if err != nil || stats.Drained != 0 || len(seen.batches) != 0 || seen.acks != 0 {
		t.Fatalf("empty spool produced work: stats=%+v err=%v", stats, err)
	}
}

func TestBacklogUsesSeveralAcknowledgedServerSizedLeases(t *testing.T) {
	records := make([]DrainedRecord, controlplane.MaxUsageBatch+25)
	for i := range records {
		records[i] = record(fmt.Sprintf("aaaaaaaa-0000-0000-0000-%012d", i), "dep-1", 1, 1)
	}
	worker, seen := stubs(t, records, nil)
	stats, err := worker.RunOnce(context.Background())
	if err != nil {
		t.Fatalf("RunOnce: %v", err)
	}
	if len(seen.batches) != 2 || len(seen.batches[0]) != controlplane.MaxUsageBatch || len(seen.batches[1]) != 25 {
		t.Fatalf("wrong lease batches: %v", []int{len(seen.batches[0]), len(seen.batches[1])})
	}
	if stats.Accepted != len(records) || seen.acks != 2 {
		t.Fatalf("not every lease was resolved: stats=%+v acks=%d", stats, seen.acks)
	}
}

func TestCapacityBoundsWorkPerPassWithoutDroppingBacklog(t *testing.T) {
	records := []DrainedRecord{
		record("aaaaaaaa-0000-0000-0000-000000000001", "dep-1", 1, 1),
		record("aaaaaaaa-0000-0000-0000-000000000002", "dep-1", 2, 2),
		record("aaaaaaaa-0000-0000-0000-000000000003", "dep-1", 3, 3),
	}
	worker, seen := stubs(t, records, nil)
	worker.Capacity = 2

	first, err := worker.RunOnce(context.Background())
	if err != nil || first.Accepted != 2 || len(seen.records) != 1 {
		t.Fatalf("first pass did not respect capacity: stats=%+v err=%v", first, err)
	}
	second, err := worker.RunOnce(context.Background())
	if err != nil || second.Accepted != 1 || len(seen.records) != 0 {
		t.Fatalf("second pass lost durable backlog: stats=%+v err=%v", second, err)
	}
}

func TestLostAcknowledgementReplaysAsDuplicateThenDeletes(t *testing.T) {
	worker, seen := stubs(t, []DrainedRecord{
		record("aaaaaaaa-0000-0000-0000-000000000001", "dep-1", 10, 5),
	}, func(attempt int) (int, any) {
		if attempt == 1 {
			return http.StatusOK, map[string]any{
				"accepted": 1, "duplicates": 0, "rejected": 0, "rejections": []any{},
			}
		}
		return http.StatusOK, map[string]any{
			"accepted": 0, "duplicates": 1, "rejected": 0, "rejections": []any{},
		}
	})
	seen.ackFailures = 1

	first, err := worker.RunOnce(context.Background())
	if err != nil || first.Accepted != 1 || first.Acknowledged != 0 || first.Pending != 1 || seen.acks != 0 {
		t.Fatalf("lost acknowledgement did not retain lease or report central acceptance: stats=%+v err=%v", first, err)
	}
	second, err := worker.RunOnce(context.Background())
	if err != nil || second.Duplicates != 1 || seen.acks != 1 || len(seen.records) != 0 {
		t.Fatalf("replayed lease did not deduplicate and delete: stats=%+v err=%v", second, err)
	}
}

func TestLeaseFailureIsNonFatalAndKeepsWorkAtSource(t *testing.T) {
	worker, seen := stubs(t, nil, nil)
	worker.Source = NewDataPlane("http://127.0.0.1:1", 100*time.Millisecond)
	stats, err := worker.RunOnce(context.Background())
	if err != nil || stats.Drained != 0 || len(seen.batches) != 0 {
		t.Fatalf("source outage should defer work without killing collector: stats=%+v err=%v", stats, err)
	}
}

func TestIncompleteCentralSuccessNeverAcknowledgesTheLease(t *testing.T) {
	cases := []struct {
		name    string
		payload any
	}{
		{name: "empty object", payload: map[string]any{}},
		{name: "partial count", payload: map[string]any{
			"accepted": 0, "duplicates": 0, "rejected": 0, "rejections": []any{},
		}},
		{name: "negative count", payload: map[string]any{
			"accepted": -1, "duplicates": 2, "rejected": 0, "rejections": []any{},
		}},
		{name: "missing rejection detail", payload: map[string]any{
			"accepted": 0, "duplicates": 0, "rejected": 1, "rejections": []any{},
		}},
		{name: "out of range rejection", payload: map[string]any{
			"accepted": 0, "duplicates": 0, "rejected": 1,
			"rejections": []any{map[string]any{"index": 7, "code": "gone"}},
		}},
	}

	for _, test := range cases {
		t.Run(test.name, func(t *testing.T) {
			worker, seen := stubs(t, []DrainedRecord{
				record("aaaaaaaa-0000-0000-0000-000000000001", "dep-1", 10, 5),
			}, func(int) (int, any) { return http.StatusOK, test.payload })

			stats, err := worker.RunOnce(context.Background())
			if err != nil || stats.Pending != 1 || stats.Acknowledged != 0 {
				t.Fatalf("invalid result was not retained: stats=%+v err=%v", stats, err)
			}
			if seen.acks != 0 || len(seen.records) != 1 {
				t.Fatalf("invalid result deleted lease: acks=%d records=%d", seen.acks, len(seen.records))
			}
		})
	}
}

func TestNewCollectorDoesNotCallAnOldDestructiveDrain(t *testing.T) {
	postCalls := 0
	oldDataPlane := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.Method == http.MethodGet && r.URL.Path == "/admin/usage":
			_ = json.NewEncoder(w).Encode(map[string]any{
				"buffered": 1, "export_mode": "drain",
			})
		case r.Method == http.MethodPost && r.URL.Path == "/admin/usage/drain":
			postCalls++
			_ = json.NewEncoder(w).Encode(map[string]any{
				"records": []DrainedRecord{
					record("aaaaaaaa-0000-0000-0000-000000000001", "dep-1", 10, 5),
				},
				"count": 1,
			})
		default:
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	defer oldDataPlane.Close()

	worker, seen := stubs(t, nil, nil)
	worker.Source = NewDataPlane(oldDataPlane.URL, time.Second)
	stats, err := worker.RunOnce(context.Background())
	if err != nil || stats.Drained != 0 || postCalls != 0 || len(seen.batches) != 0 {
		t.Fatalf("new collector touched old destructive endpoint: stats=%+v posts=%d err=%v", stats, postCalls, err)
	}
}

func TestAnExistingLeaseMayExceedALoweredPassBudget(t *testing.T) {
	records := []DrainedRecord{
		record("aaaaaaaa-0000-0000-0000-000000000001", "dep-1", 1, 1),
		record("aaaaaaaa-0000-0000-0000-000000000002", "dep-1", 2, 2),
		record("aaaaaaaa-0000-0000-0000-000000000003", "dep-1", 3, 3),
	}
	worker, seen := stubs(t, records, nil)
	seen.leased = append([]DrainedRecord(nil), records...)
	worker.Capacity = 2

	stats, err := worker.RunOnce(context.Background())
	if err != nil || stats.Accepted != 3 || stats.Acknowledged != 3 {
		t.Fatalf("stable oversized lease was not resolved: stats=%+v err=%v", stats, err)
	}
	if len(seen.batches) != 1 || len(seen.batches[0]) != 3 {
		t.Fatalf("existing lease was split or abandoned: %+v", seen.batches)
	}
}

func TestMalformedAcknowledgementLeavesTheLease(t *testing.T) {
	worker, seen := stubs(t, []DrainedRecord{
		record("aaaaaaaa-0000-0000-0000-000000000001", "dep-1", 10, 5),
	}, nil)
	seen.malformedAck = true

	stats, err := worker.RunOnce(context.Background())
	if err != nil || stats.Accepted != 1 || stats.Acknowledged != 0 || stats.Pending != 1 {
		t.Fatalf("malformed ack was not reported honestly: stats=%+v err=%v", stats, err)
	}
	if seen.acks != 0 || len(seen.records) != 1 {
		t.Fatalf("malformed ack deleted the lease: acks=%d records=%d", seen.acks, len(seen.records))
	}
}

func TestInvalidLeaseEnvelopesAreRejectedBeforeForwarding(t *testing.T) {
	valid := record("aaaaaaaa-0000-0000-0000-000000000001", "dep-1", 1, 1)
	cases := []struct {
		name  string
		lease UsageLease
	}{
		{name: "count mismatch", lease: UsageLease{LeaseID: leaseID, Count: 2, Records: []DrainedRecord{valid}}},
		{name: "records without id", lease: UsageLease{Count: 1, Records: []DrainedRecord{valid}}},
		{name: "empty with id", lease: UsageLease{LeaseID: leaseID}},
		{name: "malformed lease id", lease: UsageLease{LeaseID: "no", Count: 1, Records: []DrainedRecord{valid}}},
		{name: "malformed record id", lease: UsageLease{LeaseID: leaseID, Count: 1, Records: []DrainedRecord{{RecordID: "no"}}}},
		{name: "duplicate record id", lease: UsageLease{LeaseID: leaseID, Count: 2, Records: []DrainedRecord{valid, valid}}},
	}
	for _, test := range cases {
		t.Run(test.name, func(t *testing.T) {
			if err := validateLease(test.lease); err == nil {
				t.Fatalf("invalid lease accepted: %+v", test.lease)
			}
		})
	}
}

func TestCollectorAcceptsExplicitAlreadyAcknowledgedResult(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewEncoder(w).Encode(map[string]any{
			"lease_id":             leaseID,
			"acknowledged":         1,
			"deleted":              0,
			"already_acknowledged": true,
		})
	}))
	defer server.Close()

	client := NewDataPlane(server.URL, time.Second)
	if err := client.Ack(context.Background(), leaseID, 1); err != nil {
		t.Fatalf("matching idempotent acknowledgement was rejected: %v", err)
	}
}
