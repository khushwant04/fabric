package operator

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/khushwant04/fabric/agent/internal/kube"
)

// hostServer is a fake API server holding custom resources plus the workloads the
// operator creates for them.
type hostServer struct {
	t          *testing.T
	resources  map[string]*ModelDeployment
	configMap  *configMap
	deploys    map[string]*deployment
	services   map[string]*service
	deleted    []string
	readyAfter int
	gets       int
}

func testHost() ModelHost {
	return ModelHost{
		Image:                "vllm/vllm-openai:v0.26.0",
		ServedName:           "launch-model",
		ModelRef:             "/models/launch",
		GPUs:                 1,
		MaxModelLen:          2048,
		MaxNumSeqs:           2,
		GPUMemoryUtilization: "0.80",
		EnforceEager:         true,
		DType:                "bfloat16",
		Port:                 8000,
	}
}

func newHostServer(t *testing.T, items ...ModelDeployment) (*hostServer, *kube.Client) {
	t.Helper()
	state := &hostServer{
		t:         t,
		resources: map[string]*ModelDeployment{},
		deploys:   map[string]*deployment{},
		services:  map[string]*service{},
	}
	for i := range items {
		item := items[i]
		state.resources[item.Metadata.Name] = &item
	}

	mux := http.NewServeMux()
	crPath := fmt.Sprintf("/apis/%s/%s/namespaces/%s/%s", Group, Version, namespace, Plural)
	deployPath := fmt.Sprintf("/apis/apps/v1/namespaces/%s/deployments", namespace)
	svcPath := fmt.Sprintf("/api/v1/namespaces/%s/services", namespace)
	cmPath := fmt.Sprintf("/api/v1/namespaces/%s/configmaps", namespace)

	mux.HandleFunc(crPath, func(w http.ResponseWriter, _ *http.Request) {
		list := modelDeploymentList{}
		for _, item := range state.resources {
			list.Items = append(list.Items, *item)
		}
		writeJSON(w, 200, list)
	})
	mux.HandleFunc(crPath+"/", func(w http.ResponseWriter, r *http.Request) {
		name := strings.TrimSuffix(strings.TrimPrefix(r.URL.Path, crPath+"/"), "/status")
		item, ok := state.resources[name]
		if !ok {
			writeJSON(w, 404, map[string]string{"reason": "NotFound"})
			return
		}
		if r.Method == http.MethodPatch {
			var patch struct {
				Status Status `json:"status"`
			}
			body, _ := io.ReadAll(r.Body)
			_ = json.Unmarshal(body, &patch)
			item.Status = &patch.Status
		}
		writeJSON(w, 200, item)
	})

	mux.HandleFunc(deployPath, func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodPost {
			var created deployment
			body, _ := io.ReadAll(r.Body)
			if err := json.Unmarshal(body, &created); err != nil {
				state.t.Fatalf("decode deployment: %v", err)
			}
			state.deploys[created.Metadata.Name] = &created
			writeJSON(w, 201, created)
			return
		}
		// List, used by pruning. The label selector is not evaluated: every managed
		// host carries the labels, and the test asserts on what was deleted.
		list := struct {
			Items []deployment `json:"items"`
		}{}
		for _, d := range state.deploys {
			list.Items = append(list.Items, *d)
		}
		writeJSON(w, 200, list)
	})

	mux.HandleFunc(deployPath+"/", func(w http.ResponseWriter, r *http.Request) {
		name := strings.TrimPrefix(r.URL.Path, deployPath+"/")
		existing, ok := state.deploys[name]
		switch r.Method {
		case http.MethodGet:
			if !ok {
				writeJSON(w, 404, map[string]string{"reason": "NotFound"})
				return
			}
			state.gets++
			// Readiness appears only after the configured number of reads, standing in
			// for a server that takes minutes to load weights.
			if state.gets >= state.readyAfter && state.readyAfter > 0 {
				existing.Status = &struct {
					ReadyReplicas int `json:"readyReplicas,omitempty"`
					Replicas      int `json:"replicas,omitempty"`
				}{ReadyReplicas: 1, Replicas: 1}
			}
			writeJSON(w, 200, existing)
		case http.MethodPatch:
			if !ok {
				writeJSON(w, 404, map[string]string{"reason": "NotFound"})
				return
			}
			writeJSON(w, 200, existing)
		case http.MethodDelete:
			delete(state.deploys, name)
			state.deleted = append(state.deleted, name)
			writeJSON(w, 200, map[string]string{"status": "Success"})
		}
	})

	mux.HandleFunc(svcPath, func(w http.ResponseWriter, r *http.Request) {
		var created service
		body, _ := io.ReadAll(r.Body)
		_ = json.Unmarshal(body, &created)
		state.services[created.Metadata.Name] = &created
		writeJSON(w, 201, created)
	})
	mux.HandleFunc(svcPath+"/", func(w http.ResponseWriter, r *http.Request) {
		name := strings.TrimPrefix(r.URL.Path, svcPath+"/")
		existing, ok := state.services[name]
		if r.Method == http.MethodDelete {
			delete(state.services, name)
			writeJSON(w, 200, map[string]string{"status": "Success"})
			return
		}
		if !ok {
			writeJSON(w, 404, map[string]string{"reason": "NotFound"})
			return
		}
		writeJSON(w, 200, existing)
	})

	mux.HandleFunc(cmPath, func(w http.ResponseWriter, r *http.Request) {
		var created configMap
		body, _ := io.ReadAll(r.Body)
		_ = json.Unmarshal(body, &created)
		state.configMap = &created
		writeJSON(w, 201, created)
	})
	mux.HandleFunc(cmPath+"/", func(w http.ResponseWriter, r *http.Request) {
		if state.configMap == nil {
			writeJSON(w, 404, map[string]string{"reason": "NotFound"})
			return
		}
		if r.Method == http.MethodPut {
			var updated configMap
			body, _ := io.ReadAll(r.Body)
			_ = json.Unmarshal(body, &updated)
			state.configMap = &updated
		}
		writeJSON(w, 200, state.configMap)
	})

	server := httptest.NewServer(mux)
	t.Cleanup(server.Close)
	return state, &kube.Client{BaseURL: server.URL, Namespace: namespace, HTTP: server.Client()}
}

func hostReconciler(client *kube.Client, host ModelHost) *Reconciler {
	return New(client, Options{Namespace: namespace, Log: discardLogger(), ModelHost: host})
}

func TestNoHostIsCreatedWithoutAnImage(t *testing.T) {
	// The default remains a stamp pointing at an upstream somebody else operates,
	// which is what every existing deployment relies on.
	state, client := newHostServer(t, resource("alpha", "dep-a", "acct-a", "alpha-model", 1))

	result, err := hostReconciler(client, ModelHost{}).ReconcileOnce(context.Background())
	if err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	if len(state.deploys) != 0 || result.HostsTotal != 0 {
		t.Fatalf("a host was created with no image configured: %+v", state.deploys)
	}
	entries := decodeConfig(t, state.configMap)
	if entries[0].UpstreamURL != "http://model-host:8000" {
		t.Fatalf("the declared upstream was overridden: %q", entries[0].UpstreamURL)
	}
}

func TestADeclaredDeploymentGetsAModelHost(t *testing.T) {
	state, client := newHostServer(t, resource("alpha", "dep-a", "acct-a", "alpha-model", 1))

	if _, err := hostReconciler(client, testHost()).ReconcileOnce(context.Background()); err != nil {
		t.Fatalf("reconcile: %v", err)
	}

	host, present := state.deploys["fabric-host-dep-a"]
	if !present {
		t.Fatalf("no model host was created: %v", state.deploys)
	}
	if _, present := state.services["fabric-host-dep-a"]; !present {
		t.Fatal("no Service was created, so the data plane would have nothing to reach")
	}

	encoded, _ := json.Marshal(host.Spec)
	body := string(encoded)
	for _, expected := range []string{
		"--served-model-name=launch-model", "--max-model-len=2048",
		"--gpu-memory-utilization=0.80", "--enforce-eager", "--dtype=bfloat16",
		"nvidia.com/gpu",
	} {
		if !strings.Contains(body, expected) {
			t.Fatalf("host spec is missing %q", expected)
		}
	}
	// Two replicas would both want the GPU and the new one would never schedule.
	if !strings.Contains(body, `"type":"Recreate"`) {
		t.Fatal("rolling updates would deadlock on the GPU")
	}
}

func TestTheDataPlaneIsPointedAtTheOperatorsHost(t *testing.T) {
	// The operator owns the host, so it is the only component that knows the address.
	state, client := newHostServer(t, resource("alpha", "dep-a", "acct-a", "alpha-model", 1))

	if _, err := hostReconciler(client, testHost()).ReconcileOnce(context.Background()); err != nil {
		t.Fatalf("reconcile: %v", err)
	}

	entries := decodeConfig(t, state.configMap)
	want := fmt.Sprintf("http://fabric-host-dep-a.%s.svc:8000", namespace)
	if entries[0].UpstreamURL != want {
		t.Fatalf("upstream = %q, want %q", entries[0].UpstreamURL, want)
	}
}

func TestHostReadinessIsReportedSeparatelyFromConfiguration(t *testing.T) {
	// Configuration can be correct while the server is still loading weights, which
	// takes minutes. Collapsing them would claim a deployment is serving when it is not.
	state, client := newHostServer(t, resource("alpha", "dep-a", "acct-a", "alpha-model", 1))
	state.readyAfter = 0 // never ready
	subject := hostReconciler(client, testHost())

	result, err := subject.ReconcileOnce(context.Background())
	if err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	if result.HostsReady != 0 || result.HostsTotal != 1 {
		t.Fatalf("unexpected readiness: %+v", result)
	}

	status := state.resources["alpha"].Status
	byType := map[string]Condition{}
	for _, c := range status.Conditions {
		byType[c.Type] = c
	}
	if byType[ConditionApplied].Status != "True" {
		t.Fatal("configuration was applied, so Applied should be true")
	}
	if byType[ConditionHostReady].Status != "False" ||
		byType[ConditionHostReady].Reason != "ModelHostStarting" {
		t.Fatalf("host readiness is overstated: %+v", byType[ConditionHostReady])
	}
	if status.Phase != "pending" {
		t.Fatalf("phase = %q while the host cannot answer", status.Phase)
	}
}

func TestHostBecomingReadyIsReported(t *testing.T) {
	state, client := newHostServer(t, resource("alpha", "dep-a", "acct-a", "alpha-model", 1))
	state.readyAfter = 1
	subject := hostReconciler(client, testHost())

	result, err := subject.ReconcileOnce(context.Background())
	if err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	if result.HostsReady != 1 {
		t.Fatalf("readiness was not observed: %+v", result)
	}

	byType := map[string]Condition{}
	for _, c := range state.resources["alpha"].Status.Conditions {
		byType[c.Type] = c
	}
	if byType[ConditionHostReady].Reason != "ModelHostServing" {
		t.Fatalf("unexpected reason: %+v", byType[ConditionHostReady])
	}
	if state.resources["alpha"].Status.Phase != "ready" {
		t.Fatal("phase should be ready once the host answers")
	}
	// The reason must not claim only configuration when a host was started too.
	if byType[ConditionApplied].Reason != "ModelHostAndConfigurationApplied" {
		t.Fatalf("applied reason understates what happened: %q", byType[ConditionApplied].Reason)
	}
}

func TestAWithdrawnDeploymentsHostIsRemoved(t *testing.T) {
	// A GPU-holding workload left behind would block the next placement, and an
	// operator that restarted would have forgotten it existed.
	state, client := newHostServer(t, resource("alpha", "dep-a", "acct-a", "alpha-model", 1))
	subject := hostReconciler(client, testHost())

	if _, err := subject.ReconcileOnce(context.Background()); err != nil {
		t.Fatalf("first pass: %v", err)
	}
	delete(state.resources, "alpha")

	if _, err := subject.ReconcileOnce(context.Background()); err != nil {
		t.Fatalf("second pass: %v", err)
	}
	if len(state.deploys) != 0 {
		t.Fatalf("the host outlived its deployment: %v", state.deploys)
	}
	if len(state.services) != 0 {
		t.Fatalf("the Service outlived its deployment: %v", state.services)
	}
}

func TestAForeignWorkloadIsNotDeleted(t *testing.T) {
	// Pruning is by label. Anything without the deployment label cannot be correlated
	// and must be left alone rather than deleted on a guess.
	state, client := newHostServer(t)
	state.deploys["someone-elses-server"] = &deployment{
		Metadata: Metadata{Name: "someone-elses-server", Namespace: namespace},
	}

	if _, err := hostReconciler(client, testHost()).ReconcileOnce(context.Background()); err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	if _, present := state.deploys["someone-elses-server"]; !present {
		t.Fatal("an unrelated workload was deleted")
	}
}

func TestNodeSelectorParsing(t *testing.T) {
	selector, err := ParseNodeSelector([]string{"nvidia.com/gpu.present=true", "zone=a"})
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if selector["nvidia.com/gpu.present"] != "true" || selector["zone"] != "a" {
		t.Fatalf("unexpected selector: %v", selector)
	}

	// A malformed entry fails loudly: a silently dropped selector looks like a
	// scheduler problem much later.
	if _, err := ParseNodeSelector([]string{"missing-value"}); err == nil {
		t.Fatal("a selector without a value was accepted")
	}
}

func TestTolerationParsing(t *testing.T) {
	tolerations, err := ParseTolerations([]string{
		"nvidia.com/gpu:NoSchedule", "dedicated=inference:NoExecute",
	})
	if err != nil {
		t.Fatalf("parse: %v", err)
	}

	// A taint with no value is the common "this node has a GPU" shape, and Equal with an
	// empty value would not match it.
	if tolerations[0]["operator"] != "Exists" || tolerations[0]["effect"] != "NoSchedule" {
		t.Fatalf("unexpected toleration: %v", tolerations[0])
	}
	if tolerations[1]["operator"] != "Equal" || tolerations[1]["value"] != "inference" {
		t.Fatalf("unexpected toleration: %v", tolerations[1])
	}

	if _, err := ParseTolerations([]string{"no-effect"}); err == nil {
		t.Fatal("a toleration without an effect was accepted")
	}
}

func TestSchedulingIsAppliedToTheHostPod(t *testing.T) {
	state, client := newHostServer(t, resource("alpha", "dep-a", "acct-a", "alpha-model", 1))
	host := testHost()
	host.NodeSelector = map[string]string{"nvidia.com/gpu.present": "true"}
	host.Tolerations, _ = ParseTolerations([]string{"nvidia.com/gpu:NoSchedule"})
	host.RuntimeClassName = "nvidia"
	host.SpreadAcrossNodes = true

	if _, err := hostReconciler(client, host).ReconcileOnce(context.Background()); err != nil {
		t.Fatalf("reconcile: %v", err)
	}

	encoded, _ := json.Marshal(state.deploys["fabric-host-dep-a"].Spec)
	body := string(encoded)
	for _, expected := range []string{
		"nvidia.com/gpu.present", "tolerations", "runtimeClassName", "podAntiAffinity",
	} {
		if !strings.Contains(body, expected) {
			t.Fatalf("host pod is missing %q", expected)
		}
	}
	// Preferred, not required: on a single-node stamp a hard rule would leave the
	// second deployment permanently Pending, which is worse than sharing a node.
	if !strings.Contains(body, "preferredDuringSchedulingIgnoredDuringExecution") {
		t.Fatal("anti-affinity is required rather than preferred")
	}
}

func TestWeightsAreCachedOnTheNodeByDefault(t *testing.T) {
	// A single shared ReadWriteOnce claim was the previous behaviour and could not attach
	// to two nodes, so a second host on a second node would never start. Weights are
	// large, immutable, and only useful to a pod already on that node.
	state, client := newHostServer(t, resource("alpha", "dep-a", "acct-a", "alpha-model", 1))

	if _, err := hostReconciler(client, testHost()).ReconcileOnce(context.Background()); err != nil {
		t.Fatalf("reconcile: %v", err)
	}

	encoded, _ := json.Marshal(state.deploys["fabric-host-dep-a"].Spec)
	body := string(encoded)
	if !strings.Contains(body, DefaultCacheHostPath) {
		t.Fatalf("weights are not cached on the node: %s", body)
	}
	if strings.Contains(body, "persistentVolumeClaim") {
		t.Fatal("a claim was used when the node's own disk was available")
	}
	// The download must land in the mount rather than on the container filesystem.
	if !strings.Contains(body, "HF_HOME") || !strings.Contains(body, modelCacheMountPath) {
		t.Fatalf("the cache location is not pointed at the mount: %s", body)
	}
}

func TestACacheClaimIsUsedOnlyInPvcMode(t *testing.T) {
	state, client := newHostServer(t, resource("alpha", "dep-a", "acct-a", "alpha-model", 1))
	host := testHost()
	host.CacheMode = "pvc"
	host.CacheClaim = "weights"

	if _, err := hostReconciler(client, host).ReconcileOnce(context.Background()); err != nil {
		t.Fatalf("reconcile: %v", err)
	}

	encoded, _ := json.Marshal(state.deploys["fabric-host-dep-a"].Spec)
	if !strings.Contains(string(encoded), `"claimName":"weights"`) {
		t.Fatalf("the claim was ignored in pvc mode: %s", encoded)
	}
}

func TestCacheCanBeDisabled(t *testing.T) {
	// Correct where no local disk exists, at the cost of refetching on every start.
	state, client := newHostServer(t, resource("alpha", "dep-a", "acct-a", "alpha-model", 1))
	host := testHost()
	host.CacheMode = "none"

	if _, err := hostReconciler(client, host).ReconcileOnce(context.Background()); err != nil {
		t.Fatalf("reconcile: %v", err)
	}

	encoded, _ := json.Marshal(state.deploys["fabric-host-dep-a"].Spec)
	body := string(encoded)
	if strings.Contains(body, "hostPath") || strings.Contains(body, "persistentVolumeClaim") {
		t.Fatalf("expected an ephemeral cache: %s", body)
	}
}

func TestStartupIsGatedByAProbeRatherThanAGuessedDelay(t *testing.T) {
	// A fixed liveness delay plus a failure threshold is a deadline, and a T4 compiling
	// graphs for a cold model overran it: the container was restarted mid-startup and
	// lost the compilation it had done, which turned one slow start into a loop.
	state, client := newHostServer(t, resource("alpha", "dep-a", "acct-a", "alpha-model", 1))

	if _, err := hostReconciler(client, testHost()).ReconcileOnce(context.Background()); err != nil {
		t.Fatalf("reconcile: %v", err)
	}

	encoded, _ := json.Marshal(state.deploys["fabric-host-dep-a"].Spec)
	body := string(encoded)
	if !strings.Contains(body, "startupProbe") {
		t.Fatalf("startup is not gated by a probe: %s", body)
	}
	// Liveness must not carry its own long delay, or it becomes the deadline again.
	if strings.Contains(body, "initialDelaySeconds\":300") {
		t.Fatal("liveness still guesses how long startup takes")
	}
	// Compiled graphs are as expensive to reproduce as weights and just as identical.
	if !strings.Contains(body, "VLLM_CACHE_ROOT") {
		t.Fatalf("compilation is not cached on the node: %s", body)
	}
}

func TestOnlyAnExplicitRequestSelectsTheFabricKernel(t *testing.T) {
	// The control plane has accepted a kernel choice per deployment since the beginning
	// and nothing acted on it, so a deployment could ask for the Fabric kernel and be
	// served by the model server's own.
	for mode, expected := range map[string]bool{
		"fabric":   true,
		"standard": false,
		"auto":     false,
		"":         false,
	} {
		resource := resource("alpha", "dep-a", "acct-a", "alpha-model", 1)
		resource.Spec.KernelMode = mode
		state, client := newHostServer(t, resource)

		if _, err := hostReconciler(client, testHost()).ReconcileOnce(context.Background()); err != nil {
			t.Fatalf("reconcile with mode %q: %v", mode, err)
		}

		encoded, _ := json.Marshal(state.deploys["fabric-host-dep-a"].Spec)
		got := strings.Contains(string(encoded), "FABRIC_KERNEL")
		if got != expected {
			t.Fatalf("mode %q: substitution enabled = %v, want %v", mode, got, expected)
		}
	}
}
