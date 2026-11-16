package agent

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"
	"time"

	"github.com/khushwant04/fabric/agent/internal/servingmetrics"
	"github.com/khushwant04/fabric/agent/internal/state"
)

type oneShotMetrics struct {
	report servingmetrics.Report
	result chan error
}

func (r *oneShotMetrics) Run(ctx context.Context, send servingmetrics.Sender) {
	r.result <- send(ctx, r.report)
}

func TestServingMetricsGoToTheStampsEndpointWithTheCurrentCredential(t *testing.T) {
	var mu sync.Mutex
	var path, authorization string
	var body map[string]any
	status := http.StatusOK
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		defer mu.Unlock()
		path, authorization = r.URL.Path, r.Header.Get("Authorization")
		raw, _ := io.ReadAll(r.Body)
		_ = json.Unmarshal(raw, &body)
		w.WriteHeader(status)
		_, _ = w.Write([]byte(`{}`))
	}))
	defer server.Close()

	run := func() error {
		reporter := &oneShotMetrics{
			report: servingmetrics.Report{Samples: []servingmetrics.Sample{{
				ObservedAt: time.Date(2026, 11, 16, 10, 0, 0, 0, time.UTC), IntervalSeconds: 30,
				Sources:     []string{"gateway"},
				Deployments: []servingmetrics.DeploymentPoint{{DeploymentID: "d", OutputTokens: 5}},
				GPUs:        []servingmetrics.GPUPoint{},
			}}},
			result: make(chan error, 1),
		}
		instance := New(Config{ControlPlaneURL: server.URL, MetricsReporter: reporter},
			slog.New(slog.NewTextHandler(io.Discard, nil)))
		instance.credentials = &state.Credentials{StampID: stampID}
		instance.setAgentCredential("fab_agent_old")
		instance.setAgentCredential("fab_agent_new")
		ctx, cancel := context.WithCancel(context.Background())
		defer cancel()
		instance.startMetricsReporter(ctx)
		select {
		case err := <-reporter.result:
			return err
		case <-time.After(5 * time.Second):
			t.Fatal("reporter never ran")
			return nil
		}
	}

	if err := run(); err != nil {
		t.Fatalf("send: %v", err)
	}
	mu.Lock()
	if path != "/v1/stamps/"+stampID+"/metrics" || authorization != "Bearer fab_agent_new" {
		t.Fatalf("path %q authorization %q", path, authorization)
	}
	samples, _ := body["samples"].([]any)
	if len(samples) != 1 {
		t.Fatalf("body = %v", body)
	}
	status = http.StatusNotFound
	mu.Unlock()
	if err := run(); !errors.Is(err, servingmetrics.ErrNotAccepted) {
		t.Fatalf("404 should mean not accepted, got %v", err)
	}
	mu.Lock()
	status = http.StatusUnprocessableEntity
	mu.Unlock()
	if err := run(); !errors.Is(err, servingmetrics.ErrRejected) {
		t.Fatalf("422 should mean rejected, got %v", err)
	}
}
