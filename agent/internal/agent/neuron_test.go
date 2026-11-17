package agent

import (
	"context"
	"encoding/json"
	"strings"
	"testing"

	"github.com/khushwant04/fabric/agent/internal/activity"
	"github.com/khushwant04/fabric/agent/internal/hardware"
)

func neuronStamp(count int, instance string) hardware.Capacity {
	return hardware.Capacity{
		Measured:     true,
		PodsMeasured: true,
		GPUs: []hardware.GPUGroup{{
			Product:         "Inferentia2",
			Count:           count,
			MemoryBytes:     32 << 30,
			Source:          "node label",
			Kind:            hardware.KindNeuron,
			AcceleratorType: instance,
			NeuronCores:     2,
		}},
		AllocatableGPUs:    count,
		MaxGPUsPerNode:     count,
		MaxFreeGPUsPerNode: count,
		AvailableGPUSlots:  []int{count, count / 2, count / 3, count / 4, count / 5, count / 6, count / 7, count / 8},
	}
}

func TestANeuronStampTellsItsStoryInNeuronTerms(t *testing.T) {
	stub := &controlPlaneStub{}
	server := stub.server(t)
	instance, _ := newAgent(t, server.URL)
	instance.config.Capacity = &countingSource{capacity: neuronStamp(1, "inf2.xlarge")}

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
	if events[1].Message != "Neuron measured: 1 × Inferentia2 (inf2.xlarge, 32 GiB HBM, 2 NeuronCores)" {
		t.Errorf("measured message = %q", events[1].Message)
	}
	if events[1].Details["accelerator"] != "neuron" || events[1].Details["total_hbm_gib"] != 32.0 ||
		events[1].Details["accelerator_type"] != "inf2.xlarge" || events[1].Details["neuron_cores"] != 2 {
		t.Errorf("measured details = %v", events[1].Details)
	}
	if events[2].Severity != activity.Success ||
		events[2].Message != "Neuron runtime OK: 1 Neuron device(s) allocatable as aws.amazon.com/neuron" {
		t.Errorf("runtime = %+v", events[2])
	}
	if strings.Contains(events[2].Message, "nvidia") || strings.Contains(events[1].Message, "TPU") {
		t.Errorf("a Neuron stamp must not mention NVIDIA or TPU: %q", events[2].Message)
	}
}

func TestDescribeNeuronForSeveralDevices(t *testing.T) {
	got := describeNeuron(neuronStamp(12, "inf2.48xlarge").GPUs[0])
	want := "12 × Inferentia2 (inf2.48xlarge, 32 GiB HBM per device, 384 GiB total, 24 NeuronCores)"
	if got != want {
		t.Fatalf("describeNeuron = %q", got)
	}
}

func TestANeuronStampMissingItsDevicePluginWarnsAndWithholdsReady(t *testing.T) {
	stub := &controlPlaneStub{}
	server := stub.server(t)
	instance, _ := newAgent(t, server.URL)
	instance.config.Capacity = &countingSource{capacity: hardware.Capacity{
		Measured: true, GPUs: []hardware.GPUGroup{}, UnequippedNeuronNodes: 1,
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
			t.Fatal("a Neuron stamp without advertised devices is not ready")
		}
		if event.Kind == activity.KindGPURuntime {
			copied := event
			runtime = &copied
		}
	}
	if runtime == nil || runtime.Severity != activity.Warning ||
		!strings.Contains(runtime.Message, "Neuron runtime missing: 1 Neuron node(s)") ||
		!strings.Contains(runtime.Hint, "neuron-device-plugin") ||
		strings.Contains(runtime.Hint, "nvidia") {
		t.Fatalf("runtime = %+v", runtime)
	}
}

func TestNeuronGroupsCarryTheirAcceleratorAndOthersAreUnchanged(t *testing.T) {
	stub := &controlPlaneStub{}
	server := stub.server(t)
	instance, _ := newAgent(t, server.URL)

	instance.measured = neuronStamp(1, "inf2.xlarge")
	reported, err := json.Marshal(instance.reportedCapabilities().GPUs)
	if err != nil {
		t.Fatal(err)
	}
	want := `[{"product":"Inferentia2","count":1,"memory_bytes":34359738368,"source":"node label",` +
		`"accelerator":"neuron","vendor":"aws","accelerator_type":"inf2.xlarge"}]`
	if string(reported) != want {
		t.Fatalf("neuron report = %s", reported)
	}

	instance.measured = tpuStamp()
	reported, _ = json.Marshal(instance.reportedCapabilities().GPUs)
	want = `[{"product":"TPU v6e","count":4,"memory_bytes":34359738368,"source":"node label",` +
		`"accelerator":"tpu","vendor":"google","accelerator_type":"v6e-4"}]`
	if string(reported) != want {
		t.Fatalf("tpu report changed = %s", reported)
	}

	instance.measured = rtxProStamp()
	reported, _ = json.Marshal(instance.reportedCapabilities().GPUs)
	want = `[{"product":"NVIDIA RTX PRO 6000 Blackwell Server Edition","count":1,` +
		`"memory_bytes":102641958912,"compute_capability":"12.0","source":"node label"}]`
	if string(reported) != want {
		t.Fatalf("gpu report changed = %s", reported)
	}
}
