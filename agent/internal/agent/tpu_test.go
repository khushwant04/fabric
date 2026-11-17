package agent

import (
	"context"
	"encoding/json"
	"strings"
	"testing"

	"github.com/khushwant04/fabric/agent/internal/activity"
	"github.com/khushwant04/fabric/agent/internal/hardware"
)

func tpuStamp() hardware.Capacity {
	return hardware.Capacity{
		Measured:     true,
		PodsMeasured: true,
		GPUs: []hardware.GPUGroup{{
			Product:         "TPU v6e",
			Count:           4,
			MemoryBytes:     32 << 30,
			Source:          "node label",
			Kind:            hardware.KindTPU,
			AcceleratorType: "v6e-4",
		}},
		AllocatableGPUs:    4,
		MaxGPUsPerNode:     4,
		MaxFreeGPUsPerNode: 4,
		AvailableGPUSlots:  []int{4, 2, 1, 1, 0, 0, 0, 0},
	}
}

func TestATPUStampTellsItsStoryInTPUTerms(t *testing.T) {
	stub := &controlPlaneStub{}
	server := stub.server(t)
	instance, _ := newAgent(t, server.URL)
	instance.config.Capacity = &countingSource{capacity: tpuStamp()}

	if err := instance.Ensure(context.Background()); err != nil {
		t.Fatalf("ensure: %v", err)
	}
	if _, err := instance.ReconcileOnce(context.Background()); err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	events := instance.Activity().Pending()
	if got := strings.Join(kinds(events), ","); got != "enrolled,gpus_measured,gpu_runtime,ready" {
		t.Fatalf("kinds = %s", got)
	}
	if events[1].Message != "TPU measured: 4 × TPU v6e (v6e-4, 32 GiB HBM per chip, 128 GiB total)" {
		t.Errorf("measured message = %q", events[1].Message)
	}
	if events[1].Details["accelerator"] != "tpu" || events[1].Details["total_hbm_gib"] != 128.0 ||
		events[1].Details["accelerator_type"] != "v6e-4" {
		t.Errorf("measured details = %v", events[1].Details)
	}
	if events[2].Severity != activity.Success ||
		events[2].Message != "TPU runtime OK: 4 TPU chip(s) allocatable as google.com/tpu" {
		t.Errorf("runtime = %+v", events[2])
	}
	if strings.Contains(events[2].Message, "nvidia") {
		t.Errorf("a TPU stamp must not mention the NVIDIA runtime: %q", events[2].Message)
	}
}

func TestATPUStampMissingItsChipsWarnsAndWithholdsReady(t *testing.T) {
	stub := &controlPlaneStub{}
	server := stub.server(t)
	instance, _ := newAgent(t, server.URL)
	instance.config.Capacity = &countingSource{capacity: hardware.Capacity{
		Measured: true, GPUs: []hardware.GPUGroup{}, UnequippedTPUNodes: 1,
	}}
	if err := instance.Ensure(context.Background()); err != nil {
		t.Fatalf("ensure: %v", err)
	}
	if _, err := instance.ReconcileOnce(context.Background()); err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	var runtime *activity.Event
	for _, event := range instance.Activity().Pending() {
		if event.Kind == activity.KindReady {
			t.Fatal("a TPU stamp without advertised chips is not ready")
		}
		if event.Kind == activity.KindGPURuntime {
			copied := event
			runtime = &copied
		}
	}
	if runtime == nil || runtime.Severity != activity.Warning ||
		!strings.Contains(runtime.Message, "TPU runtime missing: 1 TPU node(s)") ||
		!strings.Contains(runtime.Hint, "Re-run the bootstrap command") ||
		strings.Contains(runtime.Hint, "nvidia") {
		t.Fatalf("runtime = %+v", runtime)
	}
}

func TestTPUGroupsCarryTheirAcceleratorAndGPUGroupsDoNot(t *testing.T) {
	stub := &controlPlaneStub{}
	server := stub.server(t)
	instance, _ := newAgent(t, server.URL)

	instance.measured = tpuStamp()
	reported, err := json.Marshal(instance.reportedCapabilities().GPUs)
	if err != nil {
		t.Fatal(err)
	}
	want := `[{"product":"TPU v6e","count":4,"memory_bytes":34359738368,"source":"node label",` +
		`"accelerator":"tpu","vendor":"google","accelerator_type":"v6e-4"}]`
	if string(reported) != want {
		t.Fatalf("tpu report = %s", reported)
	}

	// An NVIDIA stamp's document is byte-for-byte what it was.
	instance.measured = rtxProStamp()
	reported, err = json.Marshal(instance.reportedCapabilities().GPUs)
	if err != nil {
		t.Fatal(err)
	}
	want = `[{"product":"NVIDIA RTX PRO 6000 Blackwell Server Edition","count":1,` +
		`"memory_bytes":102641958912,"compute_capability":"12.0","source":"node label"}]`
	if string(reported) != want {
		t.Fatalf("gpu report = %s", reported)
	}
}
