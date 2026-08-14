package operator

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/khushwant04/fabric/agent/internal/kube"
)

const namespace = "fabric"

func discardLogger() *slog.Logger {
	return slog.New(slog.NewTextHandler(io.Discard, nil))
}

// apiServer is a fake Kubernetes API server holding custom resources and one ConfigMap.
type apiServer struct {
	t          *testing.T
	resources  map[string]*ModelDeployment
	configMap  *configMap
	creates    int
	updates    int
	patches    int
	deletes    int
	failStatus int
}

func newAPIServer(t *testing.T, items ...ModelDeployment) (*apiServer, *kube.Client) {
	t.Helper()
	state := &apiServer{t: t, resources: map[string]*ModelDeployment{}}
	for i := range items {
		item := items[i]
		state.resources[item.Metadata.Name] = &item
	}

	mux := http.NewServeMux()
	listPath := fmt.Sprintf("/apis/%s/%s/namespaces/%s/%s", Group, Version, namespace, Plural)
	configPath := fmt.Sprintf("/api/v1/namespaces/%s/configmaps", namespace)

	mux.HandleFunc(listPath, func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodPost {
			state.creates++
			var created ModelDeployment
			body, _ := io.ReadAll(r.Body)
			if err := json.Unmarshal(body, &created); err != nil {
				state.t.Fatalf("decode create: %v", err)
			}
			if _, clash := state.resources[created.Metadata.Name]; clash {
				writeJSON(w, 409, map[string]string{"reason": "AlreadyExists"})
				return
			}
			created.Metadata.Generation = 1
			created.Metadata.ResourceVersion = "1"
			state.resources[created.Metadata.Name] = &created
			writeJSON(w, 201, created)
			return
		}
		list := modelDeploymentList{}
		for _, item := range state.resources {
			list.Items = append(list.Items, *item)
		}
		writeJSON(w, 200, list)
	})

	mux.HandleFunc(listPath+"/", func(w http.ResponseWriter, r *http.Request) {
		name := strings.TrimSuffix(strings.TrimPrefix(r.URL.Path, listPath+"/"), "/status")
		item, present := state.resources[name]
		if !present {
			writeJSON(w, 404, map[string]string{"reason": "NotFound", "message": name})
			return
		}
		switch r.Method {
		case http.MethodGet:
			writeJSON(w, 200, item)
			return
		case http.MethodDelete:
			delete(state.resources, name)
			state.deletes++
			writeJSON(w, 200, map[string]string{"status": "Success"})
			return
		case http.MethodPut:
			state.updates++
			var updated ModelDeployment
			body, _ := io.ReadAll(r.Body)
			_ = json.Unmarshal(body, &updated)
			if updated.Metadata.ResourceVersion != item.Metadata.ResourceVersion {
				writeJSON(w, 409, map[string]string{"reason": "Conflict"})
				return
			}
			// A spec change advances the generation, as the API server does.
			updated.Metadata.Generation = item.Metadata.Generation + 1
			updated.Metadata.ResourceVersion = "2"
			updated.Status = item.Status
			state.resources[name] = &updated
			writeJSON(w, 200, updated)
			return
		}
		if state.failStatus != 0 {
			writeJSON(w, state.failStatus, map[string]string{
				"reason": "InternalError", "message": "refused",
			})
			return
		}
		state.patches++
		var patch struct {
			Status Status `json:"status"`
		}
		body, _ := io.ReadAll(r.Body)
		if err := json.Unmarshal(body, &patch); err != nil {
			state.t.Fatalf("decode status patch: %v", err)
		}
		item.Status = &patch.Status
		writeJSON(w, 200, item)
	})

	mux.HandleFunc(configPath, func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			writeJSON(w, 405, map[string]string{"reason": "MethodNotAllowed"})
			return
		}
		state.creates++
		var created configMap
		body, _ := io.ReadAll(r.Body)
		_ = json.Unmarshal(body, &created)
		created.Metadata.ResourceVersion = "1"
		state.configMap = &created
		writeJSON(w, 201, created)
	})

	mux.HandleFunc(configPath+"/", func(w http.ResponseWriter, r *http.Request) {
		switch r.Method {
		case http.MethodGet:
			if state.configMap == nil {
				writeJSON(w, 404, map[string]string{"reason": "NotFound"})
				return
			}
			writeJSON(w, 200, state.configMap)
		case http.MethodPut:
			state.updates++
			var updated configMap
			body, _ := io.ReadAll(r.Body)
			_ = json.Unmarshal(body, &updated)
			if state.configMap != nil &&
				updated.Metadata.ResourceVersion != state.configMap.Metadata.ResourceVersion {
				// Optimistic concurrency, as the real API server enforces it.
				writeJSON(w, 409, map[string]string{"reason": "Conflict"})
				return
			}
			updated.Metadata.ResourceVersion = "2"
			state.configMap = &updated
			writeJSON(w, 200, updated)
		default:
			writeJSON(w, 405, map[string]string{"reason": "MethodNotAllowed"})
		}
	})

	server := httptest.NewServer(mux)
	t.Cleanup(server.Close)

	return state, &kube.Client{
		BaseURL:   server.URL,
		Namespace: namespace,
		HTTP:      server.Client(),
	}
}

func writeJSON(w http.ResponseWriter, status int, body any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(body)
}

func resource(name, deploymentID, account, alias string, generation int64) ModelDeployment {
	return ModelDeployment{
		Metadata: Metadata{Name: name, Namespace: namespace, Generation: generation},
		Spec: Spec{
			DeploymentID:  deploymentID,
			AccountID:     account,
			ModelAlias:    alias,
			UpstreamURL:   "http://model-host:8000",
			UpstreamModel: "release-1",
			Generation:    int(generation),
		},
	}
}

func reconciler(client *kube.Client) *Reconciler {
	return New(client, Options{Namespace: namespace, Log: discardLogger()})
}

func decodeConfig(t *testing.T, state *apiServer) []dataPlaneEntry {
	t.Helper()
	if state.configMap == nil {
		t.Fatal("no configuration was written")
	}
	var body struct {
		Deployments []dataPlaneEntry `json:"deployments"`
	}
	if err := json.Unmarshal([]byte(state.configMap.Data["deployments.json"]), &body); err != nil {
		t.Fatalf("decode configuration: %v", err)
	}
	return body.Deployments
}

func TestDeclaredDeploymentBecomesDataPlaneConfiguration(t *testing.T) {
	state, client := newAPIServer(t, resource("alpha", "dep-a", "acct-a", "alpha-model", 1))

	result, err := reconciler(client).ReconcileOnce(context.Background())
	if err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	if result.Declared != 1 || result.Serving != 1 || !result.ConfigChanged {
		t.Fatalf("unexpected result: %+v", result)
	}

	entries := decodeConfig(t, state)
	if len(entries) != 1 {
		t.Fatalf("expected one entry, got %d", len(entries))
	}
	// The owning account travels through, which is what lets the data plane authorize
	// per account on a stamp that serves several.
	if entries[0].AccountID != "acct-a" || entries[0].ModelAlias != "alpha-model" {
		t.Fatalf("wrong entry: %+v", entries[0])
	}
}

func TestStatusReportsWhatWasActuallyDone(t *testing.T) {
	state, client := newAPIServer(t, resource("alpha", "dep-a", "acct-a", "alpha-model", 3))

	if _, err := reconciler(client).ReconcileOnce(context.Background()); err != nil {
		t.Fatalf("reconcile: %v", err)
	}

	item := state.resources["alpha"]
	if item.Status == nil {
		t.Fatal("no status was written")
	}
	if item.Status.ObservedGeneration != 3 {
		t.Fatalf("observed generation = %d, want 3", item.Status.ObservedGeneration)
	}
	condition := item.Status.Conditions[0]
	if condition.Type != ConditionApplied || condition.Status != "True" {
		t.Fatalf("unexpected condition: %+v", condition)
	}
	// No model host is started, so a reason implying a running workload would be a
	// false claim about the cluster.
	if condition.Reason != "DataPlaneConfigurationRendered" {
		t.Fatalf("reason overstates what happened: %q", condition.Reason)
	}
}

func TestAnUnchangedSetIsNotRewritten(t *testing.T) {
	state, client := newAPIServer(t, resource("alpha", "dep-a", "acct-a", "alpha-model", 1))
	subject := reconciler(client)

	if _, err := subject.ReconcileOnce(context.Background()); err != nil {
		t.Fatalf("first pass: %v", err)
	}
	second, err := subject.ReconcileOnce(context.Background())
	if err != nil {
		t.Fatalf("second pass: %v", err)
	}

	if second.ConfigChanged {
		t.Fatal("an identical document was rewritten, which churns every mounted copy")
	}
	if second.StatusWrites != 0 {
		t.Fatal("status was rewritten for an unchanged resource")
	}
	if state.updates != 0 {
		t.Fatalf("expected no configmap updates, got %d", state.updates)
	}
}

func TestEntriesAreSortedSoAnUnchangedSetIsStable(t *testing.T) {
	state, client := newAPIServer(t,
		resource("zeta", "dep-z", "acct-a", "zeta-model", 1),
		resource("alpha", "dep-a", "acct-a", "alpha-model", 1),
	)

	if _, err := reconciler(client).ReconcileOnce(context.Background()); err != nil {
		t.Fatalf("reconcile: %v", err)
	}

	entries := decodeConfig(t, state)
	if entries[0].DeploymentID != "dep-a" || entries[1].DeploymentID != "dep-z" {
		t.Fatalf("entries are not sorted: %+v", entries)
	}
}

func TestADeletingResourceIsWithdrawnImmediately(t *testing.T) {
	going := resource("going", "dep-g", "acct-a", "going-model", 1)
	going.Metadata.DeletionTimestamp = time.Now().UTC().Format(time.RFC3339)
	state, client := newAPIServer(t,
		resource("staying", "dep-s", "acct-a", "staying-model", 1),
		going,
	)

	result, err := reconciler(client).ReconcileOnce(context.Background())
	if err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	if result.Declared != 2 || result.Serving != 1 {
		t.Fatalf("unexpected result: %+v", result)
	}

	entries := decodeConfig(t, state)
	if len(entries) != 1 || entries[0].DeploymentID != "dep-s" {
		t.Fatalf("a deleting deployment is still served: %+v", entries)
	}
}

func TestConfigurationIsWrittenEvenWhenStatusFails(t *testing.T) {
	state, client := newAPIServer(t, resource("alpha", "dep-a", "acct-a", "alpha-model", 1))
	state.failStatus = http.StatusInternalServerError

	result, err := reconciler(client).ReconcileOnce(context.Background())
	if err != nil {
		t.Fatalf("a status failure must not fail the pass: %v", err)
	}
	if !result.ConfigChanged || result.StatusWrites != 0 {
		t.Fatalf("unexpected result: %+v", result)
	}
	// The data plane is already serving; refusing to configure it because a status
	// write failed would take away working capacity for a reporting problem.
	if len(decodeConfig(t, state)) != 1 {
		t.Fatal("configuration was not written")
	}
}

func TestStatusIsRewrittenWhenTheGenerationMoves(t *testing.T) {
	state, client := newAPIServer(t, resource("alpha", "dep-a", "acct-a", "alpha-model", 1))
	subject := reconciler(client)

	if _, err := subject.ReconcileOnce(context.Background()); err != nil {
		t.Fatalf("first pass: %v", err)
	}
	before := state.patches

	// The agent updates the resource: a new generation means the old verdict no longer
	// describes it.
	state.resources["alpha"].Metadata.Generation = 2
	state.resources["alpha"].Spec.ModelAlias = "alpha-model-v2"

	if _, err := subject.ReconcileOnce(context.Background()); err != nil {
		t.Fatalf("second pass: %v", err)
	}
	if state.patches <= before {
		t.Fatal("status was not refreshed for a new generation")
	}
	if state.resources["alpha"].Status.ObservedGeneration != 2 {
		t.Fatalf("observed generation = %d, want 2",
			state.resources["alpha"].Status.ObservedGeneration)
	}
}

func TestAnEmptyClusterRendersAnEmptyDocument(t *testing.T) {
	state, client := newAPIServer(t)

	if _, err := reconciler(client).ReconcileOnce(context.Background()); err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	// An empty list, not an absent file: the data plane distinguishes "nothing is
	// placed here" from "the configuration is missing".
	if entries := decodeConfig(t, state); len(entries) != 0 {
		t.Fatalf("expected an empty document, got %+v", entries)
	}
}

func TestConfigurationUpdateCarriesTheResourceVersion(t *testing.T) {
	state, client := newAPIServer(t, resource("alpha", "dep-a", "acct-a", "alpha-model", 1))
	subject := reconciler(client)

	if _, err := subject.ReconcileOnce(context.Background()); err != nil {
		t.Fatalf("first pass: %v", err)
	}
	state.resources["beta"] = ptr(resource("beta", "dep-b", "acct-b", "beta-model", 1))

	if _, err := subject.ReconcileOnce(context.Background()); err != nil {
		t.Fatalf("second pass: %v", err)
	}
	// The fake server rejects a mismatched resourceVersion with 409, so reaching here
	// proves the update carried the version it read.
	if state.updates != 1 {
		t.Fatalf("expected one update, got %d", state.updates)
	}
	if len(decodeConfig(t, state)) != 2 {
		t.Fatal("the second deployment was not added")
	}
}

func ptr(item ModelDeployment) *ModelDeployment { return &item }
