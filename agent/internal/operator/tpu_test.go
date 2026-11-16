package operator

import (
	"context"
	"encoding/json"
	"strings"
	"testing"
)

func tpuHost() ModelHost {
	host := testHost()
	host.Image = "vllm/vllm-tpu:v0.26.0"
	host.Accelerator = AcceleratorTPU
	host.Privileged = true
	host.DevicePaths = []string{"/dev/vfio"}
	host.Command = []string{"vllm", "serve"}
	host.EnforceEager = false
	// A chart or bootstrap default that came from a GPU stamp must not reach the server.
	host.RuntimeClassName = "nvidia"
	return host
}

type renderedPod struct {
	Template struct {
		Spec struct {
			RuntimeClassName string `json:"runtimeClassName"`
			Containers       []struct {
				Image           string   `json:"image"`
				Command         []string `json:"command"`
				Args            []string `json:"args"`
				SecurityContext *struct {
					Privileged bool `json:"privileged"`
				} `json:"securityContext"`
				Resources struct {
					Limits   map[string]string `json:"limits"`
					Requests map[string]string `json:"requests"`
				} `json:"resources"`
				StartupProbe struct {
					FailureThreshold int `json:"failureThreshold"`
				} `json:"startupProbe"`
				VolumeMounts []struct {
					Name      string `json:"name"`
					MountPath string `json:"mountPath"`
				} `json:"volumeMounts"`
				Env []struct {
					Name  string `json:"name"`
					Value string `json:"value"`
				} `json:"env"`
			} `json:"containers"`
			Volumes []struct {
				Name     string `json:"name"`
				HostPath *struct {
					Path string `json:"path"`
				} `json:"hostPath"`
			} `json:"volumes"`
		} `json:"spec"`
	} `json:"template"`
}

func renderHost(t *testing.T, host ModelHost, gpus int) renderedPod {
	t.Helper()
	item := resource("alpha", "dep-a", "acct-a", "alpha-model", 1)
	item.Spec.GPUCount = gpus
	state, client := newHostServer(t, item)
	if _, err := hostReconciler(client, host).ReconcileOnce(context.Background()); err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	encoded, err := json.Marshal(state.deploys["fabric-host-dep-a"].Spec)
	if err != nil {
		t.Fatal(err)
	}
	var pod renderedPod
	if err := json.Unmarshal(encoded, &pod); err != nil {
		t.Fatal(err)
	}
	if len(pod.Template.Spec.Containers) != 1 {
		t.Fatalf("containers = %s", encoded)
	}
	return pod
}

func TestATPUHostRequestsChipsAndRunsPrivilegedWithTheDevices(t *testing.T) {
	pod := renderHost(t, tpuHost(), 4)
	container := pod.Template.Spec.Containers[0]

	if container.Image != "vllm/vllm-tpu:v0.26.0" || strings.Join(container.Command, " ") != "vllm serve" {
		t.Fatalf("image/command = %q %q", container.Image, container.Command)
	}
	if container.Resources.Limits["google.com/tpu"] != "4" || container.Resources.Requests["google.com/tpu"] != "4" {
		t.Fatalf("tpu chips not requested: %+v", container.Resources)
	}
	if _, present := container.Resources.Limits["nvidia.com/gpu"]; present {
		t.Fatal("a TPU host must not request nvidia.com/gpu")
	}
	if _, present := container.Resources.Requests["nvidia.com/gpu"]; present {
		t.Fatal("a TPU host must not request nvidia.com/gpu")
	}
	if pod.Template.Spec.RuntimeClassName != "" {
		t.Fatalf("a TPU host must not use a RuntimeClass, got %q", pod.Template.Spec.RuntimeClassName)
	}
	if container.SecurityContext == nil || !container.SecurityContext.Privileged {
		t.Fatal("a TPU host must be privileged to open the chips")
	}
	if container.StartupProbe.FailureThreshold != tpuStartupFailureThreshold ||
		tpuStartupFailureThreshold <= startupFailureThreshold {
		t.Fatalf("startup budget = %d, want the longer TPU budget", container.StartupProbe.FailureThreshold)
	}
	args := strings.Join(container.Args, " ")
	if !strings.Contains(args, "--tensor-parallel-size=4") || !strings.Contains(args, "--model=release-1") {
		t.Fatalf("tensor parallelism must span the chips: %s", args)
	}

	mounted := false
	for _, mount := range container.VolumeMounts {
		if mount.MountPath == "/dev/vfio" {
			mounted = true
		}
	}
	hostPath := false
	for _, volume := range pod.Template.Spec.Volumes {
		if volume.HostPath != nil && volume.HostPath.Path == "/dev/vfio" {
			hostPath = true
		}
	}
	if !mounted || !hostPath {
		t.Fatalf("device nodes not mounted: %+v %+v", container.VolumeMounts, pod.Template.Spec.Volumes)
	}
	xla := false
	for _, env := range container.Env {
		if env.Name == "VLLM_XLA_CACHE_PATH" && env.Value == modelCacheMountPath+"/xla" {
			xla = true
		}
	}
	if !xla {
		t.Fatal("XLA compilations must be cached on the node next to the weights")
	}
}

func TestAGPUHostIsUnchangedByTPUSupport(t *testing.T) {
	host := testHost()
	host.RuntimeClassName = "nvidia"
	pod := renderHost(t, host, 2)
	container := pod.Template.Spec.Containers[0]
	if container.Resources.Limits["nvidia.com/gpu"] != "2" || container.Resources.Requests["nvidia.com/gpu"] != "2" {
		t.Fatalf("gpu request changed: %+v", container.Resources)
	}
	if _, present := container.Resources.Limits["google.com/tpu"]; present {
		t.Fatal("a GPU host requested TPU chips")
	}
	if pod.Template.Spec.RuntimeClassName != "nvidia" || container.SecurityContext != nil || container.Command != nil {
		t.Fatalf("gpu pod changed: %+v", pod.Template.Spec)
	}
	if container.StartupProbe.FailureThreshold != startupFailureThreshold {
		t.Fatalf("gpu startup budget changed: %d", container.StartupProbe.FailureThreshold)
	}
	for _, env := range container.Env {
		if env.Name == "VLLM_XLA_CACHE_PATH" {
			t.Fatal("a GPU host got TPU environment")
		}
	}
}

func TestATPUProfileForcesBFloat16(t *testing.T) {
	host := tpuHost()
	host.DType = "float16"
	profiles := []GPUProfile{{Node: "tpu-01", Model: "TPU v6e", MemoryMiB: 32 * 1024, Count: 4, Kind: "tpu"}}
	adjusted, changes := applyProfile(host, profiles)
	if adjusted.DType != "bfloat16" || len(changes) != 1 || changes[0].Setting != "dtype" {
		t.Fatalf("dtype = %q, changes = %+v", adjusted.DType, changes)
	}

	host.DType = "bfloat16"
	if adjusted, changes := applyProfile(host, profiles); adjusted.DType != "bfloat16" || len(changes) != 0 {
		t.Fatalf("a correct dtype was changed: %+v", changes)
	}
}

func TestTPUNodesAreProfiledForTheOperator(t *testing.T) {
	getter := &fakeGetter{payload: `{"items":[{
		"metadata":{"name":"tpu-01","labels":{"fabric.khushwant.dev/tpu-accelerator-type":"v5litepod-4"}},
		"status":{"conditions":[{"type":"Ready","status":"True"}],"allocatable":{"google.com/tpu":"4"}}}]}`}
	profiles, err := ProfileGPUNodes(context.Background(), getter, map[string]string{"accelerator": "tpu"})
	if err != nil {
		t.Fatal(err)
	}
	if len(profiles) != 1 || !profiles[0].IsTPU() || profiles[0].Model != "TPU v5e" ||
		profiles[0].MemoryMiB != 16*1024 || profiles[0].Count != 4 {
		t.Fatalf("profiles = %+v", profiles)
	}
	// Memory headroom is judged per chip, as it is per GPU.
	if got := smallestMemory(profiles); got != 16*1024 {
		t.Fatalf("smallest = %d", got)
	}
}
