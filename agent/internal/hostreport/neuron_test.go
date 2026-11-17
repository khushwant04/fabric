package hostreport

import (
	"strings"
	"testing"
)

func TestDiagnoseNamesNeuronStartupFailures(t *testing.T) {
	cases := map[string]struct {
		log    string
		reason string
		hint   string
	}{
		"missing device": {
			"RuntimeError: No neuron devices found. Please check the Neuron driver.",
			"NeuronRuntimeMissing", "aws.amazon.com/neuron",
		},
		"runtime init": {
			"2026-Nov-17 ERROR NRT:nrt_init  nrt_init failed: unable to open /dev/neuron0",
			"NeuronRuntimeMissing", "neuron-device-plugin",
		},
		"compiler": {
			"ERROR: neuronx-cc returned non-zero exit status 70 while compiling the model",
			"NeuronCompilationFailed", "ahead of time",
		},
		"out of device memory": {
			"ERROR NRT:nrt_allocate_tensor  Failed to allocate 4294967296 bytes on device: NRT_RESOURCE",
			"NeuronOutOfMemory", "more Neuron devices",
		},
	}
	for name, item := range cases {
		diagnosis, failing := Diagnose(Pod{ContainerReason: "CrashLoopBackOff", RestartCount: 1, Log: item.log})
		if !failing || diagnosis.Reason != item.reason || !strings.Contains(diagnosis.Hint, item.hint) {
			t.Errorf("%s: %+v (failing=%v)", name, diagnosis, failing)
		}
	}
}

func TestNeuronPatternsDoNotCaptureGPUOrTPUFailures(t *testing.T) {
	cases := map[string]string{
		"RuntimeError: Failed to infer device type":                           "GPURuntimeMissing",
		"torch.OutOfMemoryError: CUDA out of memory.":                         "GPUOutOfMemory",
		"triton.compiler.errors.CompilationError: at 1:0: compile failed":     "",
		"RuntimeError: TPU initialization failed: No ba16c7433 device found.": "TPURuntimeMissing",
	}
	for log, reason := range cases {
		diagnosis, _ := Diagnose(Pod{ContainerReason: "CrashLoopBackOff", RestartCount: 1, Log: log})
		if strings.HasPrefix(diagnosis.Reason, "Neuron") {
			t.Errorf("%q diagnosed as %s", log, diagnosis.Reason)
		}
		if reason != "" && diagnosis.Reason != reason {
			t.Errorf("%q = %s, want %s", log, diagnosis.Reason, reason)
		}
	}
}
