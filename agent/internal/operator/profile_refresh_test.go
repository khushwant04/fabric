package operator

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/khushwant04/fabric/agent/internal/kube"
)

func TestHardwareProfileRetriesAndTracksPoolChanges(t *testing.T) {
	var nodes []map[string]any
	status, reads := http.StatusOK, 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, request *http.Request) {
		if request.URL.Path != "/api/v1/nodes" {
			t.Fatalf("unexpected read: %s", request.URL.Path)
		}
		reads++
		writeJSON(w, status, map[string]any{"items": nodes})
	}))
	defer server.Close()
	client := &kube.Client{BaseURL: server.URL, HTTP: server.Client()}
	host := testHost()
	host.GPUMemoryUtilization = "0.99"
	subject := hostReconciler(client, host)
	now := time.Unix(1000, 0)
	profileNode := func(name, model, memory string) map[string]any {
		return map[string]any{
			"metadata": map[string]any{"name": name, "labels": map[string]string{
				"nvidia.com/gpu.product": model, "nvidia.com/gpu.memory": memory,
			}},
			"status": map[string]any{"conditions": []map[string]string{{"type": "Ready", "status": "True"}}, "allocatable": map[string]string{"nvidia.com/gpu": "1"}},
		}
	}
	refresh := func(after time.Duration) { subject.applyHardwareProfileAt(context.Background(), now.Add(after)) }
	refresh(0)
	refresh(time.Second)
	if reads != 1 || subject.profileFingerprint != "" {
		t.Fatalf("empty profile must be bounded and remain retryable: reads=%d", reads)
	}
	status = http.StatusForbidden
	refresh(15 * time.Second)
	if subject.profileFingerprint != "" {
		t.Fatal("a failed read must not freeze profiling")
	}
	status = http.StatusOK
	nodes = []map[string]any{profileNode("strong", "NVIDIA-A100", "40960")}
	refresh(30 * time.Second)
	if subject.options.ModelHost.DType != "bfloat16" || subject.smallestMemoryMiB != 40960 {
		t.Fatalf("successful retry did not describe the pool: %+v", subject.options.ModelHost)
	}
	strongFingerprint := subject.profileFingerprint
	refresh(31 * time.Second)
	if reads != 3 {
		t.Fatalf("successful refresh must be bounded for one minute: reads=%d", reads)
	}
	nodes = append(nodes, profileNode("weak", "Tesla-T4", "16384"))
	refresh(90 * time.Second)
	if subject.options.ModelHost.DType != "float16" || subject.options.ModelHost.GPUMemoryUtilization != "0.93" || subject.smallestMemoryMiB != 16384 {
		t.Fatalf("joining weaker GPU did not adjust future host specs: %+v", subject.options.ModelHost)
	}
	fingerprint := subject.profileFingerprint
	nodes[0], nodes[1] = nodes[1], nodes[0]
	refresh(150 * time.Second)
	if subject.profileFingerprint != fingerprint {
		t.Fatal("node list ordering must not change the pool fingerprint")
	}
	status = http.StatusInternalServerError
	refresh(210 * time.Second)
	if subject.options.ModelHost.DType != "float16" || subject.profileFingerprint != fingerprint {
		t.Fatal("transient read failure discarded the last safe profile")
	}
	status = http.StatusOK
	nodes = []map[string]any{profileNode("strong", "NVIDIA-A100", "40960")}
	refresh(225 * time.Second)
	if subject.options.ModelHost.DType != "bfloat16" || subject.profileFingerprint != strongFingerprint || subject.options.ModelHost.GPUMemoryUtilization != "0.97" {
		t.Fatalf("removing weak hardware must recompute from original settings: %+v", subject.options.ModelHost)
	}
}

func TestAnAppliedReleaseLosingAllHostsIsDegraded(t *testing.T) {
	item := resource("alpha", "dep-a", "acct-a", "alpha-model", 1)
	state, client := newHostServer(t, item)
	subject := hostReconciler(client, testHost())
	rollout := &RolloutStatus{Phase: rolloutPhaseServing, ActiveRelease: releaseOf(item)}
	if err := subject.reportApplied(context.Background(), item, modelHostReadiness{Desired: 1}, RolloutDecision{}, rollout); err != nil {
		t.Fatal(err)
	}
	status := state.resources["alpha"].Status
	if status.Phase != "degraded" || status.ReadyReplicas == nil || *status.ReadyReplicas != 0 {
		t.Fatalf("an applied but unavailable release must not appear ready: %+v", status)
	}
	conditions := map[string]Condition{}
	for _, condition := range status.Conditions {
		conditions[condition.Type] = condition
	}
	if conditions[ConditionApplied].Status != "True" || conditions[ConditionAvailable].Status != "False" || status.Rollout.ActiveRelease != releaseOf(item) {
		t.Fatal("availability must not erase the applied release")
	}
	if err := subject.reportApplied(context.Background(), item, modelHostReadiness{Desired: 1, Ready: 1}, RolloutDecision{}, rollout); err != nil {
		t.Fatal(err)
	}
	if state.resources["alpha"].Status.Phase != "ready" {
		t.Fatal("a recovered applied release must become ready")
	}
}

func TestUnavailableHealthReasonReachesTheAgent(t *testing.T) {
	item := resource("alpha", "dep-a", "acct-a", "alpha-model", 1)
	item.Metadata.Labels = map[string]string{"fabric.khushwant.dev/stamp-id": stampID}
	item.Status = &Status{Phase: "ready", ObservedGeneration: 1, Conditions: []Condition{
		{Type: ConditionApplied, Status: "True", Reason: "ModelHostAndConfigurationApplied"},
		{Type: ConditionAvailable, Status: "False", Reason: "NoReadyModelHost", Message: "Host is recovering"},
	}}
	_, client := newAPIServer(t, item)
	observed, err := NewPublisher(client, namespace, stampID).ObservedConditions(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if got := observed["dep-a"]; !got.Applied || got.Phase != "degraded" || got.Reason != "NoReadyModelHost" || got.Message != "Host is recovering" {
		t.Fatalf("availability must reach the agent without losing Applied: %+v", got)
	}
}

func TestTextOnlyAndResourceRequestsAreExplicit(t *testing.T) {
	host := testHost()
	host.TextOnly = true
	host.TextOnlyModels = []string{"Qwen/Qwen3.5-0.8B"}
	host.CPURequest, host.MemoryRequest = "2", "12Gi"
	subject := hostReconciler(nil, host)
	qwen := resource("qwen", "dep-q", "acct-a", "Qwen/Qwen3.5-0.8B", 1)
	qwen.Spec.UpstreamModel = "Qwen/Qwen3.5-0.8B"
	other := resource("other", "dep-o", "acct-a", "vision-model", 1)
	for _, item := range []ModelDeployment{qwen, other} {
		data, _ := json.Marshal(subject.desiredHost(item))
		var document struct {
			Spec struct {
				Template struct {
					Spec struct {
						Containers []struct {
							Args      []string `json:"args"`
							Resources struct {
								Requests map[string]any `json:"requests"`
								Limits   map[string]any `json:"limits"`
							} `json:"resources"`
						} `json:"containers"`
					} `json:"spec"`
				} `json:"template"`
			} `json:"spec"`
		}
		if err := json.Unmarshal(data, &document); err != nil {
			t.Fatal(err)
		}
		container := document.Spec.Template.Spec.Containers[0]
		textOnly := false
		for _, arg := range container.Args {
			if arg == "--language-model-only" {
				textOnly = true
			}
		}
		if textOnly != (item.Spec.ModelAlias == "Qwen/Qwen3.5-0.8B") {
			t.Fatalf("text-only must respect the exact model allowlist: %s", data)
		}
		if container.Resources.Requests["cpu"] != "2" || container.Resources.Requests["memory"] != "12Gi" || container.Resources.Limits["cpu"] != nil || container.Resources.Limits["memory"] != nil {
			t.Fatalf("resource reservations must not throttle CPU or add OOM limits: %s", data)
		}
	}
	if testHost().TextOnly || testHost().CPURequest != "" || testHost().MemoryRequest != "" {
		t.Fatal("new policies must remain opt-in")
	}
}

func TestTextOnlyRoutingUsesTheLoadedReleaseAndPreservesDeclarations(t *testing.T) {
	qwen := resource("qwen", "dep-q", "acct-a", "short-chat-name", 1)
	qwen.Spec.UpstreamModel = "Qwen/Qwen3.5-0.8B"
	qwen.Spec.Capabilities = &ModelCapabilities{
		AutoEnabled: true, Chat: true, Completion: true, Vision: true,
		Transcription: true, Translation: true, Code: true, Reasoning: true, Priority: 8,
	}
	vision := resource("vision", "dep-v", "acct-a", "Qwen/Qwen3.5-0.8B", 1)
	vision.Spec.UpstreamModel = "other/vision-model"
	vision.Spec.Capabilities = qwen.Spec.Capabilities
	state, client := newHostServer(t, qwen, vision)
	host := testHost()
	host.TextOnly = true
	host.TextOnlyModels = []string{"Qwen/Qwen3.5-0.8B"}
	if _, err := hostReconciler(client, host).ReconcileOnce(context.Background()); err != nil {
		t.Fatal(err)
	}
	var config struct {
		Deployments []dataPlaneEntry `json:"deployments"`
	}
	if err := json.Unmarshal([]byte(state.configMap.Data["deployments.json"]), &config); err != nil {
		t.Fatal(err)
	}
	for _, route := range config.Deployments {
		capabilities := route.Capabilities
		if capabilities == nil || !capabilities.Chat || !capabilities.Completion || !capabilities.AutoEnabled || !capabilities.Code || !capabilities.Reasoning || capabilities.Priority != 8 {
			t.Fatalf("text routing capabilities were lost: %+v", route)
		}
		if restricted := route.DeploymentID == "dep-q"; capabilities.Vision == restricted || capabilities.Transcription == restricted || capabilities.Translation == restricted {
			t.Fatalf("routing must match the actual loaded model rather than its alias: %+v", route)
		}
	}
	if !state.resources["qwen"].Spec.Capabilities.Vision || !qwen.Spec.Capabilities.Vision {
		t.Fatal("effective routing policy modified the declared resource")
	}
}

func TestTextOnlyRoutingAccountsForBothRolloutReleases(t *testing.T) {
	host := testHost()
	host.TextOnly = true
	host.TextOnlyModels = []string{"Qwen/Qwen3.5-0.8B"}
	subject := hostReconciler(nil, host)
	item := resource("vision", "dep-v", "acct-a", "vision-chat", 1)
	item.Spec.UpstreamModel = "other/vision-model"
	item.Spec.Capabilities = &ModelCapabilities{Chat: true, Vision: true}
	decision := RolloutDecision{ActiveRelease: "Qwen/Qwen3.5-0.8B", CandidateRelease: "other/vision-model"}
	if subject.routedCapabilities(item, decision).Vision {
		t.Fatal("a mixed rollout must not route vision to a text-only active backend")
	}
	decision.ActiveRelease = decision.CandidateRelease
	if !subject.routedCapabilities(item, decision).Vision {
		t.Fatal("a fully multimodal fleet must retain vision")
	}
	host.Image = ""
	if !hostReconciler(nil, host).routedCapabilities(item, RolloutDecision{}).Vision {
		t.Fatal("the managed-host policy must not restrict an externally operated endpoint")
	}
}

func TestAnAppliedReleaseWithPartialReadinessIsDegraded(t *testing.T) {
	item := resource("alpha", "dep-a", "acct-a", "alpha-model", 1)
	state, client := newHostServer(t, item)
	rollout := &RolloutStatus{Phase: rolloutPhaseServing, ActiveRelease: releaseOf(item)}
	if err := hostReconciler(client, testHost()).reportApplied(context.Background(), item, modelHostReadiness{Desired: 2, Ready: 1}, RolloutDecision{}, rollout); err != nil {
		t.Fatal(err)
	}
	status := state.resources["alpha"].Status
	if status.Phase != "degraded" || status.ReadyReplicas == nil || *status.ReadyReplicas != 1 || status.UnavailableReplicas == nil || *status.UnavailableReplicas != 1 {
		t.Fatalf("partial capacity loss must be visible without hiding the healthy replica: %+v", status)
	}
}

func TestTritonCompilationArtifactsUseTheConfiguredCacheVolume(t *testing.T) {
	for _, mode := range []string{"hostPath", "pvc", "none"} {
		t.Run(mode, func(t *testing.T) {
			host := testHost()
			host.CacheMode, host.CacheClaim, host.EnforceEager = mode, "weights", true
			host.CacheHostPath = "/mnt/fabric/model-cache"
			item := resource("qwen", "dep-q", "acct-a", "Qwen/Qwen3.5-0.8B", 1)
			data, err := json.Marshal(hostReconciler(nil, host).desiredHost(item))
			if err != nil {
				t.Fatal(err)
			}
			var document struct {
				Spec struct {
					Template struct {
						Spec struct {
							Containers []struct {
								Env          []struct{ Name, Value string }
								VolumeMounts []struct{ Name, MountPath string }
							}
							Volumes []map[string]any
						}
					}
				}
			}
			if err := json.Unmarshal(data, &document); err != nil {
				t.Fatal(err)
			}
			container := document.Spec.Template.Spec.Containers[0]
			environment := map[string]string{}
			for _, entry := range container.Env {
				environment[entry.Name] = entry.Value
			}
			if environment["TRITON_CACHE_DIR"] != "/model-cache/triton" {
				t.Fatalf("eager Triton artifacts would be lost on container replacement: %+v", environment)
			}
			mounted := false
			for _, mount := range container.VolumeMounts {
				mounted = mounted || mount.Name == "cache" && mount.MountPath == "/model-cache"
			}
			if !mounted {
				t.Fatal("the Triton cache path is not on the configured cache volume")
			}
			for _, volume := range document.Spec.Template.Spec.Volumes {
				if volume["name"] != "cache" {
					continue
				}
				wantType := map[string]string{"hostPath": "hostPath", "pvc": "persistentVolumeClaim", "none": "emptyDir"}[mode]
				if volume[wantType] == nil {
					t.Fatalf("Triton cache must honor explicitly selected persistence: %+v", volume)
				}
			}
		})
	}
}

func TestManagedHostCredentialsUseReferencesAndNoAPIToken(t *testing.T) {
	host := testHost()
	host.ServiceAccountName = "stamp-model-host"
	host.ImagePullSecrets = []string{"registry-credentials"}
	host.HuggingFaceSecret, host.HuggingFaceTokenKey = "hf-credentials", "access-token"
	item := resource("qwen", "dep-q", "acct-a", "Qwen/Qwen3.5-2B", 1)
	data, err := json.Marshal(hostReconciler(nil, host).desiredHost(item))
	if err != nil {
		t.Fatal(err)
	}
	var document struct {
		Spec struct {
			Template struct {
				Spec struct {
					ServiceAccountName           string
					AutomountServiceAccountToken bool
					ImagePullSecrets             []map[string]string
					Containers                   []struct {
						Env []struct {
							Name      string
							Value     string
							ValueFrom struct{ SecretKeyRef map[string]string }
						}
					}
				}
			}
		}
	}
	if err := json.Unmarshal(data, &document); err != nil {
		t.Fatal(err)
	}
	pod := document.Spec.Template.Spec
	if pod.ServiceAccountName != "stamp-model-host" || pod.AutomountServiceAccountToken || len(pod.ImagePullSecrets) != 1 || pod.ImagePullSecrets[0]["name"] != "registry-credentials" {
		t.Fatalf("model host inherited API credentials or lost registry references: %s", data)
	}
	found := false
	for _, entry := range pod.Containers[0].Env {
		if entry.Name != "HF_TOKEN" {
			continue
		}
		found = true
		if entry.Value != "" || entry.ValueFrom.SecretKeyRef["name"] != "hf-credentials" || entry.ValueFrom.SecretKeyRef["key"] != "access-token" {
			t.Fatalf("Hugging Face token must be a kubelet-resolved Secret reference: %s", data)
		}
	}
	if !found {
		t.Fatal("Hugging Face Secret reference missing")
	}
}
