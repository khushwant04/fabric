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
	t             *testing.T
	resources     map[string]*ModelDeployment
	configMap     *configMap
	deploys       map[string]*deployment
	services      map[string]*service
	deleted       []string
	readyAfter    int
	readyReplicas int
	gets          int
	// endpoints maps a host name to the pod endpoints its headless Service selects,
	// stood in for the EndpointSlices the operator reads.
	endpoints map[string][]endpointAddress
}

// endpointAddress is one pod endpoint in the fake, with its readiness and identity.
type endpointAddress struct {
	Address     string
	AddressType string
	Ready       bool
	PodUID      string
	PodName     string
	Namespace   string
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
		endpoints: map[string][]endpointAddress{},
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
				readyReplicas := state.readyReplicas
				if readyReplicas < 1 {
					readyReplicas = 1
				}
				existing.Status = &struct {
					ReadyReplicas int `json:"readyReplicas,omitempty"`
					Replicas      int `json:"replicas,omitempty"`
				}{ReadyReplicas: readyReplicas, Replicas: readyReplicas}
			}
			writeJSON(w, 200, existing)
		case http.MethodPatch:
			if !ok {
				writeJSON(w, 404, map[string]string{"reason": "NotFound"})
				return
			}
			// Apply the merge patch to the stored object so a later observe reads the new
			// release annotation, as the real API server would. Only the fields the
			// operator patches (spec, metadata labels/annotations) are merged.
			var patch struct {
				Spec     map[string]any `json:"spec"`
				Metadata struct {
					Labels      map[string]string `json:"labels"`
					Annotations map[string]string `json:"annotations"`
				} `json:"metadata"`
			}
			body, _ := io.ReadAll(r.Body)
			_ = json.Unmarshal(body, &patch)
			if patch.Spec != nil {
				existing.Spec = patch.Spec
			}
			if patch.Metadata.Labels != nil {
				existing.Metadata.Labels = patch.Metadata.Labels
			}
			if patch.Metadata.Annotations != nil {
				existing.Metadata.Annotations = patch.Metadata.Annotations
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

	esPath := fmt.Sprintf("/apis/discovery.k8s.io/v1/namespaces/%s/endpointslices", namespace)
	mux.HandleFunc(esPath, func(w http.ResponseWriter, r *http.Request) {
		// The label selector names the Service, so the slices returned are the ones for
		// that host. It arrives URL-encoded (kubernetes.io/service-name%3D<name>).
		selector := r.URL.Query().Get("labelSelector")
		serviceName := ""
		if _, value, found := strings.Cut(selector, "kubernetes.io/service-name="); found {
			serviceName = value
		}
		list := endpointSliceList{}
		if addresses, ok := state.endpoints[serviceName]; ok {
			for _, addr := range addresses {
				addressType := addr.AddressType
				if addressType == "" {
					addressType = "IPv4"
				}
				slice := endpointSlice{AddressType: addressType}
				ready := addr.Ready
				endpoint := endpointSliceEndpoint{Addresses: []string{addr.Address}}
				endpoint.Conditions.Ready = &ready
				if addr.PodUID != "" || addr.PodName != "" {
					endpoint.TargetRef = &struct {
						Kind      string `json:"kind,omitempty"`
						Namespace string `json:"namespace,omitempty"`
						Name      string `json:"name,omitempty"`
						UID       string `json:"uid,omitempty"`
					}{
						Kind: "Pod", Namespace: addr.Namespace,
						Name: addr.PodName, UID: addr.PodUID,
					}
				}
				slice.Endpoints = append(slice.Endpoints, endpoint)
				list.Items = append(list.Items, slice)
			}
		}
		writeJSON(w, 200, list)
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

func TestAPartiallyReadyFleetStaysPending(t *testing.T) {
	item := resource("alpha", "dep-a", "acct-a", "alpha-model", 1)
	item.Spec.Replicas = 3
	state, client := newHostServer(t, item)
	state.readyAfter = 1
	state.readyReplicas = 1

	if _, err := hostReconciler(client, testHost()).ReconcileOnce(context.Background()); err != nil {
		t.Fatalf("reconcile: %v", err)
	}

	status := state.resources["alpha"].Status
	if status.Phase != "pending" {
		t.Fatalf("phase = %q, want pending while only part of the fleet is ready", status.Phase)
	}
	if status.ReadyReplicas == nil || *status.ReadyReplicas != 1 {
		t.Fatalf("ready replicas = %v, want 1", status.ReadyReplicas)
	}
	if status.UnavailableReplicas == nil || *status.UnavailableReplicas != 2 {
		t.Fatalf("unavailable replicas = %v, want 2", status.UnavailableReplicas)
	}
	for _, condition := range status.Conditions {
		if condition.Type == ConditionHostReady && condition.Status != "False" {
			t.Fatalf("partial fleet reported ready: %+v", condition)
		}
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

func TestReplicaCountComesFromTheDeployment(t *testing.T) {
	// The operator hardcoded a single replica, so a deployment could ask for a fleet and
	// get one host (ADR 0010). The rendered Deployment must carry the requested count.
	item := resource("alpha", "dep-a", "acct-a", "alpha-model", 1)
	item.Spec.Replicas = 4
	state, client := newHostServer(t, item)

	if _, err := hostReconciler(client, testHost()).ReconcileOnce(context.Background()); err != nil {
		t.Fatalf("reconcile: %v", err)
	}

	host := state.deploys["fabric-host-dep-a"]
	if host == nil {
		t.Fatalf("no model host was created: %v", state.deploys)
	}
	if got := host.Spec["replicas"]; fmt.Sprintf("%v", got) != "4" {
		t.Fatalf("replicas = %v, want 4", got)
	}
	// Recreate is kept for now: M3 replaces the rollout strategy.
	encoded, _ := json.Marshal(host.Spec)
	if !strings.Contains(string(encoded), `"type":"Recreate"`) {
		t.Fatal("the rollout strategy changed unexpectedly")
	}
}

func TestReplicaCountDefaultsToOneWhenUnset(t *testing.T) {
	// A deployment that predates the replicas field carries zero, which must mean the
	// single replica it has always had rather than none.
	state, client := newHostServer(t, resource("alpha", "dep-a", "acct-a", "alpha-model", 1))

	if _, err := hostReconciler(client, testHost()).ReconcileOnce(context.Background()); err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	if got := state.deploys["fabric-host-dep-a"].Spec["replicas"]; fmt.Sprintf("%v", got) != "1" {
		t.Fatalf("replicas = %v, want 1 by default", got)
	}
}

func TestTheHostServiceIsHeadless(t *testing.T) {
	// A headless Service exposes the pod endpoints individually so the data plane can
	// discover and balance across them, rather than hiding them behind one ClusterIP
	// that kube-proxy spreads opaquely (ADR 0010).
	state, client := newHostServer(t, resource("alpha", "dep-a", "acct-a", "alpha-model", 1))

	if _, err := hostReconciler(client, testHost()).ReconcileOnce(context.Background()); err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	svc := state.services["fabric-host-dep-a"]
	if svc == nil {
		t.Fatal("no Service was created")
	}
	if svc.Spec["clusterIP"] != "None" {
		t.Fatalf("service is not headless: clusterIP = %v", svc.Spec["clusterIP"])
	}
}

func TestAnExistingClusterIPServiceIsMigratedToHeadless(t *testing.T) {
	state, client := newHostServer(t, resource("alpha", "dep-a", "acct-a", "alpha-model", 1))
	state.services["fabric-host-dep-a"] = &service{
		Metadata: Metadata{Name: "fabric-host-dep-a", Namespace: namespace},
		Spec: map[string]any{
			"clusterIP": "10.96.0.25",
			"selector":  map[string]any{"old": "selector"},
		},
	}

	if _, err := hostReconciler(client, testHost()).ReconcileOnce(context.Background()); err != nil {
		t.Fatalf("reconcile: %v", err)
	}

	svc := state.services["fabric-host-dep-a"]
	if svc == nil || svc.Spec["clusterIP"] != "None" {
		t.Fatalf("legacy Service was not recreated as headless: %#v", svc)
	}
}

func TestBackendsArePublishedFromReadyEndpoints(t *testing.T) {
	// The operator publishes the concrete pod endpoints as a backends list so the data
	// plane balances across them (ADR 0010). An unready endpoint is skipped.
	state, client := newHostServer(t, resource("alpha", "dep-a", "acct-a", "alpha-model", 1))
	state.endpoints["fabric-host-dep-a"] = []endpointAddress{
		{Address: "10.0.0.1", Ready: true},
		{Address: "10.0.0.2", Ready: true},
		{Address: "10.0.0.3", Ready: false},
	}

	if _, err := hostReconciler(client, testHost()).ReconcileOnce(context.Background()); err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	entries := decodeConfig(t, state.configMap)
	if len(entries) != 1 {
		t.Fatalf("expected one deployment entry, got %d", len(entries))
	}
	urls := make([]string, 0, len(entries[0].Backends))
	for _, backend := range entries[0].Backends {
		urls = append(urls, backend.URL)
	}
	want := []string{"http://10.0.0.1:8000", "http://10.0.0.2:8000"}
	if fmt.Sprintf("%v", urls) != fmt.Sprintf("%v", want) {
		t.Fatalf("published backends = %v, want %v (the unready endpoint must be skipped)", urls, want)
	}
	// upstream_url stays populated so a data plane that predates the pool still reaches a
	// live host.
	if entries[0].UpstreamURL != "http://10.0.0.1:8000" {
		t.Fatalf("upstream_url = %q, want the first backend", entries[0].UpstreamURL)
	}
}

func TestBackendsUsePodIdentityAndRenderIPv6Safely(t *testing.T) {
	// EndpointSlice targetRef is the stable pod identity. Dual-stack slices may report
	// the same pod twice, so publish it once (preferring IPv4) while still formatting an
	// IPv6-only pod as a valid bracketed URL.
	state, client := newHostServer(t, resource("alpha", "dep-a", "acct-a", "alpha-model", 1))
	state.endpoints["fabric-host-dep-a"] = []endpointAddress{
		{Address: "2001:db8::10", AddressType: "IPv6", Ready: true, PodUID: "pod-a"},
		{Address: "10.0.0.10", AddressType: "IPv4", Ready: true, PodUID: "pod-a"},
		{Address: "2001:db8::20", AddressType: "IPv6", Ready: true, PodUID: "pod-b"},
	}

	if _, err := hostReconciler(client, testHost()).ReconcileOnce(context.Background()); err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	entries := decodeConfig(t, state.configMap)
	if len(entries[0].Backends) != 2 {
		t.Fatalf("backends = %v, want one address per pod", entries[0].Backends)
	}
	want := []dataPlaneBackend{
		{ID: "pod-a", URL: "http://10.0.0.10:8000"},
		{ID: "pod-b", URL: "http://[2001:db8::20]:8000"},
	}
	for i := range want {
		if entries[0].Backends[i].ID != want[i].ID ||
			entries[0].Backends[i].URL != want[i].URL {
			t.Fatalf("backend[%d] = %+v, want %+v", i, entries[0].Backends[i], want[i])
		}
	}
}

func TestBackendsFallBackToTheServiceAddressWithoutEndpoints(t *testing.T) {
	// While the pods are still starting no endpoint is ready, so the Service DNS name is
	// published as a single backend and the deployment is served rather than handed an
	// empty pool.
	state, client := newHostServer(t, resource("alpha", "dep-a", "acct-a", "alpha-model", 1))

	if _, err := hostReconciler(client, testHost()).ReconcileOnce(context.Background()); err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	entries := decodeConfig(t, state.configMap)
	want := fmt.Sprintf("http://fabric-host-dep-a.%s.svc:8000", namespace)
	if len(entries[0].Backends) != 1 || entries[0].Backends[0].URL != want {
		t.Fatalf("fallback backend = %v, want a single %q", entries[0].Backends, want)
	}
}

// backendWeights indexes a config entry's backends by URL to their weight, so a test can
// assert how a weighted rollout split the traffic.
func backendWeights(entry dataPlaneEntry) map[string]float64 {
	weights := map[string]float64{}
	for _, backend := range entry.Backends {
		weights[backend.URL] += backend.Weight
	}
	return weights
}

func TestAReleaseChangeCoexistsBothReleasesInTheConfig(t *testing.T) {
	// The downtime-free rollout, end to end through the operator: a release change brings
	// the new release up beside the old, and the data plane config carries BOTH releases'
	// backends with weights so the weighted strategy splits traffic (ADR 0011).
	item := resource("alpha", "dep-a", "acct-a", "alpha-model", 1)
	item.Spec.UpstreamModel = "r1"
	item.Spec.Replicas = 2
	state, client := newHostServer(t, item)
	subject := hostReconciler(client, testHost())

	// First pass: the primary settles on r1 and becomes ready (readyAfter drives it), so
	// r1 is the observed-good release before the change.
	state.readyAfter = 1
	primary := "fabric-host-dep-a"
	state.endpoints[primary] = []endpointAddress{
		{Address: "10.0.0.1", Ready: true}, {Address: "10.0.0.2", Ready: true},
	}
	if _, err := subject.ReconcileOnce(context.Background()); err != nil {
		t.Fatalf("first pass: %v", err)
	}

	// Declare r2. The primary workload rolls to r2, while r1 keeps serving from its
	// sidecar. Second pass begins the rollout (primary patched to r2, r1 sidecar created).
	state.resources["alpha"].Spec.UpstreamModel = "r2"
	state.deploys[primary].Status = nil
	state.readyAfter = 0
	sidecar := "fabric-host-dep-a-" + releaseSuffix("r1")
	state.endpoints[sidecar] = []endpointAddress{{Address: "10.1.0.1", Ready: true}}
	state.endpoints[primary] = nil
	if _, err := subject.ReconcileOnce(context.Background()); err != nil {
		t.Fatalf("second pass: %v", err)
	}
	if _, ok := state.deploys[sidecar]; !ok {
		t.Fatalf("no sidecar workload was created for the old release: %v", state.deploys)
	}

	// Third pass: half of the new release's replicas (1 of 2) are ready, so traffic
	// splits between the two releases. The primary now publishes a ready r2 endpoint.
	state.deploys[primary].Status = &struct {
		ReadyReplicas int `json:"readyReplicas,omitempty"`
		Replicas      int `json:"replicas,omitempty"`
	}{ReadyReplicas: 1, Replicas: 2}
	state.readyAfter = 0
	state.endpoints[primary] = []endpointAddress{{Address: "10.2.0.1", Ready: true}}
	if _, err := subject.ReconcileOnce(context.Background()); err != nil {
		t.Fatalf("third pass: %v", err)
	}

	entries := decodeConfig(t, state.configMap)
	if len(entries) != 1 {
		t.Fatalf("expected one deployment entry, got %d", len(entries))
	}
	if entries[0].Strategy != "weighted" {
		t.Fatalf("a coexisting rollout must force the weighted strategy: %q", entries[0].Strategy)
	}
	weights := backendWeights(entries[0])
	// BOTH releases are in the pool with weight, so the data plane splits traffic.
	if weights["http://10.1.0.1:8000"] <= 0 {
		t.Fatalf("the old release is not serving during the rollout: %+v", entries[0].Backends)
	}
	if weights["http://10.2.0.1:8000"] <= 0 {
		t.Fatalf("the new release is not receiving its share: %+v", entries[0].Backends)
	}
	total := 0.0
	for _, w := range weights {
		total += w
	}
	if total <= 0 {
		t.Fatalf("the pool is empty mid-rollout, requests would be dropped: %+v", entries[0].Backends)
	}
}

func TestAWeightedRolloutCompletesAndDrainsTheOldRelease(t *testing.T) {
	// When the new release is fully ready the old release's workload is removed and the
	// config returns to a single release (ADR 0011).
	item := resource("alpha", "dep-a", "acct-a", "alpha-model", 1)
	item.Spec.UpstreamModel = "r1"
	state, client := newHostServer(t, item)
	subject := hostReconciler(client, testHost())
	primary := "fabric-host-dep-a"
	sidecar := "fabric-host-dep-a-" + releaseSuffix("r1")

	// Bring r1 up ready.
	state.readyAfter = 1
	state.endpoints[primary] = []endpointAddress{{Address: "10.0.0.1", Ready: true}}
	if _, err := subject.ReconcileOnce(context.Background()); err != nil {
		t.Fatalf("first pass: %v", err)
	}

	// Declare r2 and let it start (primary not ready), creating the r1 sidecar.
	state.resources["alpha"].Spec.UpstreamModel = "r2"
	state.deploys[primary].Status = nil
	state.readyAfter = 0
	state.endpoints[sidecar] = []endpointAddress{{Address: "10.1.0.1", Ready: true}}
	state.endpoints[primary] = nil
	if _, err := subject.ReconcileOnce(context.Background()); err != nil {
		t.Fatalf("second pass: %v", err)
	}
	if _, ok := state.deploys[sidecar]; !ok {
		t.Fatal("sidecar was not created")
	}

	// The rollout must first take effect: after the starting pass the primary is on r2.
	// Run one more pass so the primary is observed on r2 with the sidecar (the weighted
	// shift), then mark the primary fully ready and reconcile again to complete it.
	if _, err := subject.ReconcileOnce(context.Background()); err != nil {
		t.Fatalf("third pass: %v", err)
	}
	// Now the primary's r2 is fully ready. The completion pass must drain the r1 sidecar
	// and return to a single release. Set readiness directly so it is deterministic.
	state.readyAfter = 1
	state.deploys[primary].Status = &struct {
		ReadyReplicas int `json:"readyReplicas,omitempty"`
		Replicas      int `json:"replicas,omitempty"`
	}{ReadyReplicas: 1, Replicas: 1}
	state.endpoints[primary] = []endpointAddress{{Address: "10.2.0.1", Ready: true}}
	if _, err := subject.ReconcileOnce(context.Background()); err != nil {
		t.Fatalf("completion pass: %v", err)
	}

	if _, ok := state.deploys[sidecar]; ok {
		t.Fatalf("the old release's sidecar was not drained: %v", state.deploys)
	}
	entries := decodeConfig(t, state.configMap)
	urls := map[string]bool{}
	for _, backend := range entries[0].Backends {
		urls[backend.URL] = true
	}
	if urls["http://10.1.0.1:8000"] {
		t.Fatalf("the drained release still appears in the pool: %+v", entries[0].Backends)
	}
	// The config returns to a single release: the new release's endpoint, and the
	// strategy is no longer forced to weighted.
	if !urls["http://10.2.0.1:8000"] {
		t.Fatalf("the new release is not serving after completion: %+v", entries[0].Backends)
	}
	if entries[0].Strategy == "weighted" {
		t.Fatalf("the config did not return to the deployment's own strategy: %q", entries[0].Strategy)
	}
}

func TestARolloutFallsBackToRecreateWithoutSpareGPUs(t *testing.T) {
	// A stamp whose only GPU is already committed cannot coexist two releases, so the
	// operator must replace the release in place rather than stand up a sidecar that would
	// never schedule (ADR 0011). Capacity is asserted directly on the decision path.
	item := resource("alpha", "dep-a", "acct-a", "alpha-model", 1)
	item.Spec.UpstreamModel = "r1"
	item.Spec.Replicas = 1
	_, client := newHostServer(t, item)
	subject := hostReconciler(client, testHost())
	// One allocatable GPU, one committed by the single-replica deployment: no room for a
	// second release.
	subject.allocatableGPUs = 1
	subject.profiled = true

	if subject.canCoexist([]ModelDeployment{item}) {
		t.Fatal("a fully-committed single GPU stamp claimed it could coexist a rollout")
	}

	// With a spare device it can.
	subject.allocatableGPUs = 2
	if !subject.canCoexist([]ModelDeployment{item}) {
		t.Fatal("a stamp with a spare GPU refused to coexist a rollout")
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
