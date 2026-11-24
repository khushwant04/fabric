package operator

import (
	"context"
	"encoding/json"
	"strings"
	"testing"
)

const neuronTestImage = "public.ecr.aws/neuron/pytorch-inference-vllm-neuronx:0.13.0-neuronx-py312-sdk2.28.0-ubuntu24.04"

func neuronHost() ModelHost {
	host := testHost()
	host.Image = neuronTestImage
	host.Accelerator = AcceleratorNeuron
	host.Command = []string{"vllm", "serve"}
	host.NeuronCoresPerDevice = 2
	host.EnforceEager = false
	// A chart or bootstrap default that came from a GPU stamp must not reach the server.
	host.RuntimeClassName = "nvidia"
	return host
}

func envValue(t *testing.T, pod renderedPod, name string) (string, bool) {
	t.Helper()
	for _, env := range pod.Template.Spec.Containers[0].Env {
		if env.Name == name {
			return env.Value, true
		}
	}
	return "", false
}

func TestANeuronHostRequestsDevicesAndShardsAcrossTheirCores(t *testing.T) {
	pod := renderHost(t, neuronHost(), 1)
	container := pod.Template.Spec.Containers[0]

	if container.Image != neuronTestImage || strings.Join(container.Command, " ") != "vllm serve" {
		t.Fatalf("image/command = %q %q", container.Image, container.Command)
	}
	if container.Resources.Limits["aws.amazon.com/neuron"] != "1" ||
		container.Resources.Requests["aws.amazon.com/neuron"] != "1" {
		t.Fatalf("neuron devices not requested: %+v", container.Resources)
	}
	for _, other := range []string{"nvidia.com/gpu", "google.com/tpu", "aws.amazon.com/neuroncore"} {
		if _, present := container.Resources.Limits[other]; present {
			t.Fatalf("a Neuron host must not request %s", other)
		}
		if _, present := container.Resources.Requests[other]; present {
			t.Fatalf("a Neuron host must not request %s", other)
		}
	}
	if pod.Template.Spec.RuntimeClassName != "" {
		t.Fatalf("a Neuron host must not use a RuntimeClass, got %q", pod.Template.Spec.RuntimeClassName)
	}
	if container.SecurityContext != nil {
		t.Fatal("with the device plugin a Neuron host needs no privileges")
	}
	if pod.Template.Spec.HostNetwork || pod.Template.Spec.DNSPolicy != "" {
		t.Fatal("a Neuron host keeps the pod network")
	}
	if container.StartupProbe.FailureThreshold != neuronStartupFailureThreshold ||
		neuronStartupFailureThreshold <= tpuStartupFailureThreshold {
		t.Fatalf("startup budget = %d, want the longer Neuron budget", container.StartupProbe.FailureThreshold)
	}
	args := strings.Join(container.Args, " ")
	if !strings.Contains(args, "--tensor-parallel-size=2") || !strings.Contains(args, "--model=release-1") {
		t.Fatalf("one Inferentia2 device is two NeuronCores: %s", args)
	}
	if value, ok := envValue(t, pod, "NEURON_COMPILE_CACHE_URL"); !ok || value != modelCacheMountPath+"/neuron/compile-cache" {
		t.Fatalf("compile cache = %q", value)
	}
	artifacts, ok := envValue(t, pod, "NEURON_COMPILED_ARTIFACTS")
	if !ok || !strings.HasPrefix(artifacts, modelCacheMountPath+"/neuron/compiled/release-1/tp2-") {
		t.Fatalf("compiled artifacts = %q", artifacts)
	}
	if _, ok := envValue(t, pod, "VLLM_XLA_CACHE_PATH"); ok {
		t.Fatal("a Neuron host got TPU environment")
	}
}

func TestNeuronTensorParallelismCountsCoresOfEveryDevice(t *testing.T) {
	host := neuronHost()
	host.NeuronCoresPerDevice = 4 // Trainium2 at the default logical-core setting.
	pod := renderHost(t, host, 4)
	args := strings.Join(pod.Template.Spec.Containers[0].Args, " ")
	if !strings.Contains(args, "--tensor-parallel-size=16") {
		t.Fatalf("four Trainium2 devices are sixteen logical cores: %s", args)
	}
	if pod.Template.Spec.Containers[0].Resources.Limits["aws.amazon.com/neuron"] != "4" {
		t.Fatalf("devices = %+v", pod.Template.Spec.Containers[0].Resources)
	}

	host.NeuronCoresPerDevice = 0
	pod = renderHost(t, host, 1)
	if args := strings.Join(pod.Template.Spec.Containers[0].Args, " "); !strings.Contains(args, "--tensor-parallel-size=2") {
		t.Fatalf("an unset core count must default to two per device: %s", args)
	}
}

func TestANeuronHostWithoutTheDevicePluginRunsPrivilegedWithTheDevices(t *testing.T) {
	host := neuronHost()
	host.Privileged = true
	host.DevicePaths = []string{"/dev/neuron0"}
	pod := renderHost(t, host, 1)
	container := pod.Template.Spec.Containers[0]
	if container.SecurityContext == nil || !container.SecurityContext.Privileged {
		t.Fatal("without the device plugin the host must be privileged")
	}
	mounted, hostPath := false, false
	for _, mount := range container.VolumeMounts {
		if mount.Name == "neuron-device-0" && mount.MountPath == "/dev/neuron0" {
			mounted = true
		}
	}
	for _, volume := range pod.Template.Spec.Volumes {
		if volume.Name == "neuron-device-0" && volume.HostPath != nil && volume.HostPath.Path == "/dev/neuron0" {
			hostPath = true
		}
	}
	if !mounted || !hostPath {
		t.Fatalf("device nodes not mounted: %+v %+v", container.VolumeMounts, pod.Template.Spec.Volumes)
	}
}

func TestNeuronArtifactKeyChangesWithTheCompilation(t *testing.T) {
	host := neuronHost()
	host.ModelRef = "Qwen/Qwen3.5-4B"
	host.MaxModelLen = 8192
	host.MaxNumSeqs = 4
	host.DType = "bfloat16"
	base := host.neuronArtifactKey()
	if base != "qwen-qwen3.5-4b/tp2-len8192-seqs4-bfloat16" {
		t.Fatalf("key = %q", base)
	}
	for name, change := range map[string]func(*ModelHost){
		"model":   func(h *ModelHost) { h.ModelRef = "Qwen/Qwen3.5-9B" },
		"devices": func(h *ModelHost) { h.GPUs = 2 },
		"length":  func(h *ModelHost) { h.MaxModelLen = 4096 },
		"seqs":    func(h *ModelHost) { h.MaxNumSeqs = 8 },
	} {
		changed := host
		change(&changed)
		if changed.neuronArtifactKey() == base {
			t.Errorf("%s change kept the compiled artifacts", name)
		}
	}
}

func TestANeuronProfileForcesBFloat16(t *testing.T) {
	host := neuronHost()
	host.DType = "float16"
	profiles := []GPUProfile{{Node: "inf2-01", Model: "Inferentia2", MemoryMiB: 32 * 1024, Count: 1, Kind: "neuron"}}
	adjusted, changes := applyProfile(host, profiles)
	if adjusted.DType != "bfloat16" || len(changes) != 1 || changes[0].Setting != "dtype" ||
		!strings.Contains(changes[0].Reason, "Neuron") {
		t.Fatalf("dtype = %q, changes = %+v", adjusted.DType, changes)
	}
	host.DType = "bfloat16"
	if adjusted, changes := applyProfile(host, profiles); adjusted.DType != "bfloat16" || len(changes) != 0 {
		t.Fatalf("a correct dtype was changed: %+v", changes)
	}
}

func TestNeuronNodesAreProfiledForTheOperator(t *testing.T) {
	getter := &fakeGetter{payload: `{"items":[{
		"metadata":{"name":"inf2-01","labels":{"fabric.khushwant.dev/neuron-instance-type":"inf2.xlarge"}},
		"status":{"conditions":[{"type":"Ready","status":"True"}],"allocatable":{"aws.amazon.com/neuron":"1"}}}]}`}
	profiles, err := ProfileGPUNodes(context.Background(), getter, map[string]string{"accelerator": "neuron"})
	if err != nil {
		t.Fatal(err)
	}
	if len(profiles) != 1 || !profiles[0].IsNeuron() || profiles[0].Model != "Inferentia2" ||
		profiles[0].MemoryMiB != 32*1024 || profiles[0].Count != 1 {
		t.Fatalf("profiles = %+v", profiles)
	}
}

// The GPU and TPU renderings must not move at all: compare whole pod specs before and after
// rendering with Neuron-only fields set, which only a Neuron host reads.
func TestGPUAndTPUHostsIgnoreNeuronSettings(t *testing.T) {
	for name, base := range map[string]ModelHost{"gpu": testHost(), "tpu": tpuHost()} {
		plain, err := json.Marshal(renderHost(t, base, 2))
		if err != nil {
			t.Fatal(err)
		}
		withNeuron := base
		withNeuron.NeuronCoresPerDevice = 8
		got, err := json.Marshal(renderHost(t, withNeuron, 2))
		if err != nil {
			t.Fatal(err)
		}
		if string(plain) != string(got) {
			t.Fatalf("%s host changed by a Neuron setting:\n%s\n%s", name, plain, got)
		}
		if strings.Contains(string(got), "NEURON_") || strings.Contains(string(got), "aws.amazon.com") {
			t.Fatalf("%s host got Neuron rendering: %s", name, got)
		}
	}
}

func TestNeuronTurnsOffPrefixCachingUnlessConfigured(t *testing.T) {
	// vLLM 0.13 enables prefix caching by default and the Neuron plugin then refuses to start
	// without a block size.
	args := strings.Join(renderHost(t, neuronHost(), 1).Template.Spec.Containers[0].Args, " ")
	if !strings.Contains(args, "--no-enable-prefix-caching") {
		t.Fatalf("a Neuron host must turn prefix caching off by default: %s", args)
	}

	configured := neuronHost()
	configured.ExtraArgs = []string{"--enable-prefix-caching", "--block-size=32"}
	args = strings.Join(renderHost(t, configured, 1).Template.Spec.Containers[0].Args, " ")
	if strings.Contains(args, "--no-enable-prefix-caching") || !strings.Contains(args, "--block-size=32") {
		t.Fatalf("explicit prefix-caching args must be left alone: %s", args)
	}

	gpu := testHost()
	args = strings.Join(renderHost(t, gpu, 1).Template.Spec.Containers[0].Args, " ")
	if strings.Contains(args, "prefix-caching") {
		t.Fatalf("GPU hosts keep vLLM's default prefix caching: %s", args)
	}
}
