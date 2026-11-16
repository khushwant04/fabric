package activity

import (
	"context"
	"errors"
	"strings"
	"sync"
	"testing"
	"time"
)

func TestCredentialsAreMaskedBeforeBuffering(t *testing.T) {
	recorder := New(nil)
	recorder.Record(Event{
		Kind:    KindError,
		Message: "pull failed for hf_" + strings.Repeat("x", 30) + " with Bearer abc.def",
		Hint:    "token=supersecret then retry",
		Details: map[string]any{"credential": "fab_agent_0123_secret", "count": 2},
	})
	event := recorder.Pending()[0]
	for _, leaked := range []string{"hf_xxx", "abc.def", "supersecret", "fab_agent_0123"} {
		if strings.Contains(event.Message+event.Hint+event.Details["credential"].(string), leaked) {
			t.Errorf("%q survived: %+v", leaked, event)
		}
	}
	if event.Hint != "token=[redacted] then retry" || event.Details["count"] != 2 {
		t.Errorf("event = %+v", event)
	}
	if event.ID == "" || event.At.IsZero() || event.Severity != Info {
		t.Errorf("defaults not filled: %+v", event)
	}
}

func TestOrdinaryTextIsUntouched(t *testing.T) {
	for _, text := range []string{
		"1 × NVIDIA RTX PRO 6000 Blackwell Server Edition found (96 GiB)",
		"GPU runtime OK: 1 nvidia.com/gpu allocatable",
		"Applied desired-state generation 12: configured qwen-chat",
	} {
		if got := Redact(text); got != text {
			t.Errorf("Redact(%q) = %q", text, got)
		}
	}
}

func TestRecordChangeRecordsOnlyChanges(t *testing.T) {
	recorder := New(nil)
	event := Event{Kind: KindGPURuntime, Message: "GPU runtime OK"}
	if !recorder.RecordChange("runtime", "ok", event) || recorder.RecordChange("runtime", "ok", event) {
		t.Fatal("an unchanged state was recorded twice")
	}
	if !recorder.RecordChange("runtime", "missing", event) {
		t.Fatal("a changed state was not recorded")
	}
	recorder.Forget("runtime")
	if !recorder.RecordChange("runtime", "missing", event) {
		t.Fatal("a forgotten state was not recorded again")
	}
	if len(recorder.Pending()) != 3 {
		t.Fatalf("pending = %d", len(recorder.Pending()))
	}
}

func TestTheBufferIsBounded(t *testing.T) {
	recorder := New(nil)
	for index := 0; index < MaxPending+25; index++ {
		recorder.Record(Event{Kind: KindDesiredStateApplied, Message: "applied"})
	}
	if len(recorder.Pending()) != MaxPending {
		t.Fatalf("pending = %d", len(recorder.Pending()))
	}
}

func TestFlushSendsInBatchesAndKeepsWhatFailed(t *testing.T) {
	recorder := New(nil)
	for index := 0; index < MaxBatch+5; index++ {
		recorder.Record(Event{Kind: KindDesiredStateApplied, Message: "applied"})
	}
	var sizes []int
	fail := true
	send := func(_ context.Context, events []Event) error {
		if fail {
			return errors.New("unreachable")
		}
		sizes = append(sizes, len(events))
		return nil
	}
	if err := recorder.flush(context.Background(), send); err == nil {
		t.Fatal("a failed upload reported success")
	}
	if len(recorder.Pending()) != MaxBatch+5 {
		t.Fatal("a failed upload dropped events")
	}
	fail = false
	if err := recorder.flush(context.Background(), send); err != nil {
		t.Fatal(err)
	}
	if len(sizes) != 2 || sizes[0] != MaxBatch || sizes[1] != 5 || len(recorder.Pending()) != 0 {
		t.Fatalf("sizes = %v pending = %d", sizes, len(recorder.Pending()))
	}
}

func TestRunUploadsSoonAfterARecord(t *testing.T) {
	recorder := New(nil)
	var mu sync.Mutex
	var got []Event
	done := make(chan struct{})
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go recorder.Run(ctx, func(_ context.Context, events []Event) error {
		mu.Lock()
		got = append(got, events...)
		mu.Unlock()
		close(done)
		return nil
	})
	recorder.Record(Event{Kind: KindEnrolled, Message: "Enrolled"})
	select {
	case <-done:
	case <-time.After(5 * time.Second):
		t.Fatal("nothing was uploaded")
	}
	mu.Lock()
	defer mu.Unlock()
	if len(got) != 1 || got[0].Kind != KindEnrolled {
		t.Fatalf("got = %+v", got)
	}
}
