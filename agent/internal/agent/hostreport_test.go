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

	"github.com/khushwant04/fabric/agent/internal/hostreport"
	"github.com/khushwant04/fabric/agent/internal/state"
)

// oneShotReporter sends a single fixed report and hands back the send result.
type oneShotReporter struct {
	report hostreport.Report
	result chan error
}

func (r *oneShotReporter) Run(ctx context.Context, send hostreport.Sender) {
	r.result <- send(ctx, r.report)
}

func TestHostReportsGoToTheStampsEndpointWithTheCurrentCredential(t *testing.T) {
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

	run := func(reporter *oneShotReporter) error {
		instance := New(Config{ControlPlaneURL: server.URL, HostReporter: reporter},
			slog.New(slog.NewTextHandler(io.Discard, nil)))
		instance.credentials = &state.Credentials{StampID: stampID}
		// A rotation on the reconcile loop must be what the reporter presents next.
		instance.setAgentCredential("fab_agent_old")
		instance.setAgentCredential("fab_agent_new")
		ctx, cancel := context.WithCancel(context.Background())
		defer cancel()
		instance.startHostReporter(ctx)
		select {
		case err := <-reporter.result:
			return err
		case <-time.After(5 * time.Second):
			t.Fatal("reporter never ran")
			return nil
		}
	}

	report := hostreport.Report{
		ObservedAt: time.Date(2026, 11, 16, 10, 0, 0, 0, time.UTC),
		Deployments: []hostreport.Deployment{{
			DeploymentID: "dep-1",
			Pods:         []hostreport.Pod{{Name: "fabric-host-dep-1-abc", Phase: "Running"}},
		}},
	}
	if err := run(&oneShotReporter{report: report, result: make(chan error, 1)}); err != nil {
		t.Fatalf("send: %v", err)
	}
	mu.Lock()
	if path != "/v1/stamps/"+stampID+"/hosts" {
		t.Errorf("path = %s", path)
	}
	if authorization != "Bearer fab_agent_new" {
		t.Errorf("authorization = %q", authorization)
	}
	deployments, _ := body["deployments"].([]any)
	if len(deployments) != 1 {
		t.Errorf("body = %v", body)
	}
	// A control plane that predates host reports answers 404; the reporter is told so it
	// backs off instead of retrying every few seconds.
	status = http.StatusNotFound
	mu.Unlock()

	err := run(&oneShotReporter{report: report, result: make(chan error, 1)})
	if !errors.Is(err, hostreport.ErrNotAccepted) {
		t.Fatalf("404 = %v, want ErrNotAccepted", err)
	}
}
