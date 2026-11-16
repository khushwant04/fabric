package hardware

import (
	"context"
	"testing"
)

func TestParseAcceleratorTypeCountsChipsNotCores(t *testing.T) {
	cases := []struct {
		value   string
		product string
		chips   int
		hbmGiB  int
		ok      bool
	}{
		{"v6e-4", "TPU v6e", 4, 32, true},
		{"v6e-8", "TPU v6e", 8, 32, true},
		{"v5litepod-4", "TPU v5e", 4, 16, true},
		{"v5e-4", "TPU v5e", 4, 16, true},
		{"V5LITEPOD-1", "TPU v5e", 1, 16, true},
		// v4 and v5p types count TensorCores, two per chip.
		{"v4-8", "TPU v4", 4, 32, true},
		{"v5p-8", "TPU v5p", 4, 95, true},
		{"v3-8", "", 0, 0, false},
		{"v6e", "", 0, 0, false},
		{"v6e-x", "", 0, 0, false},
		{"", "", 0, 0, false},
	}
	for _, test := range cases {
		chip, chips, ok := ParseAcceleratorType(test.value)
		if ok != test.ok || chip.Product != test.product || chips != test.chips || chip.HBMMiB != test.hbmGiB*1024 {
			t.Errorf("%q = %+v, %d, %v", test.value, chip, chips, ok)
		}
	}
}

const tpuVMNode = `{"items":[
	{"metadata":{"name":"tpu-01","labels":{"accelerator":"tpu",
		"fabric.khushwant.dev/tpu-accelerator-type":"v6e-4"}},
	 "status":{"conditions":[{"type":"Ready","status":"True"}],"allocatable":{"cpu":"180","google.com/tpu":"4"}}}]}`

func TestTPUVMNodeIsMeasuredAsChipsWithHBM(t *testing.T) {
	cluster := &clusterStub{nodes: tpuVMNode, pods: `{"items":[
		{"metadata":{"name":"host","labels":{"app.kubernetes.io/managed-by":"fabric-operator",
			"fabric.khushwant.dev/deployment-id":"d1"}},
		 "spec":{"nodeName":"tpu-01","containers":[{"resources":{"limits":{"google.com/tpu":"4"}}}]},
		 "status":{"phase":"Running"}}]}`}
	capacity, err := Measure(context.Background(), cluster, map[string]string{"accelerator": "tpu"})
	if err != nil {
		t.Fatal(err)
	}
	if capacity.AllocatableGPUs != 4 || capacity.MaxGPUsPerNode != 4 || capacity.UnequippedTPUNodes != 0 {
		t.Fatalf("capacity = %+v", capacity)
	}
	if capacity.RequestedGPUs != 4 || capacity.FabricRequestedGPUs != 4 || capacity.MaxFreeGPUsPerNode != 0 {
		t.Fatalf("tpu claims were not counted: %+v", capacity)
	}
	if len(capacity.GPUs) != 1 {
		t.Fatalf("groups = %+v", capacity.GPUs)
	}
	group := capacity.GPUs[0]
	if group.Product != "TPU v6e" || group.Kind != KindTPU || group.AcceleratorType != "v6e-4" ||
		group.MemoryBytes != 32<<30 || group.ComputeCapability != "" || group.Source != "node label" {
		t.Fatalf("group = %+v", group)
	}
	if capacity.Accelerator() != KindTPU || !capacity.Profiles[0].IsTPU() {
		t.Fatalf("stamp not read as TPU: %+v", capacity)
	}
}

func TestGKETPUPoolsAreDescribedFromTheirLabels(t *testing.T) {
	cluster := &clusterStub{pods: `{"items":[]}`, nodes: `{"items":[
		{"metadata":{"name":"gke-a","labels":{"cloud.google.com/gke-tpu-accelerator":"tpu-v5-lite-podslice"}},
		 "status":{"conditions":[{"type":"Ready","status":"True"}],"allocatable":{"google.com/tpu":"4"}}},
		{"metadata":{"name":"gke-b","labels":{"node.kubernetes.io/instance-type":"ct6e-standard-4t"}},
		 "status":{"conditions":[{"type":"Ready","status":"True"}],"allocatable":{"google.com/tpu":"4"}}}]}`}
	capacity, err := Measure(context.Background(), cluster, nil)
	if err != nil {
		t.Fatal(err)
	}
	if len(capacity.GPUs) != 2 || capacity.GPUs[0].Product != "TPU v5e" || capacity.GPUs[1].Product != "TPU v6e" {
		t.Fatalf("groups = %+v", capacity.GPUs)
	}
	if capacity.GPUs[0].Source != "accelerator label tpu-v5-lite-podslice" || capacity.GPUs[1].Source != "machine family ct6e-" {
		t.Fatalf("sources = %+v", capacity.GPUs)
	}
}

func TestTPUNodeWithoutAdvertisedChipsIsUnequipped(t *testing.T) {
	cluster := &clusterStub{pods: `{"items":[]}`, nodes: `{"items":[
		{"metadata":{"name":"tpu-01","labels":{"accelerator":"tpu","fabric.khushwant.dev/tpu-accelerator-type":"v6e-4"}},
		 "status":{"conditions":[{"type":"Ready","status":"True"}],"allocatable":{"cpu":"180"}}}]}`}
	capacity, err := Measure(context.Background(), cluster, map[string]string{"accelerator": "tpu"})
	if err != nil {
		t.Fatal(err)
	}
	if capacity.AllocatableGPUs != 0 || capacity.UnequippedTPUNodes != 1 || capacity.UnequippedGPUNodes != 0 {
		t.Fatalf("capacity = %+v", capacity)
	}
	if capacity.Accelerator() != KindTPU {
		t.Fatalf("a TPU stamp missing its advertisement must still read as TPU")
	}
}

func TestUnknownTPUTypeStaysUndescribed(t *testing.T) {
	cluster := &clusterStub{pods: `{"items":[]}`, nodes: `{"items":[
		{"metadata":{"name":"tpu-01","labels":{"fabric.khushwant.dev/tpu-accelerator-type":"v9x-4"}},
		 "status":{"conditions":[{"type":"Ready","status":"True"}],"allocatable":{"google.com/tpu":"4"}}}]}`}
	capacity, err := Measure(context.Background(), cluster, nil)
	if err != nil {
		t.Fatal(err)
	}
	group := capacity.GPUs[0]
	if group.Product != "unknown TPU" || group.MemoryBytes != 0 || group.Kind != KindTPU {
		t.Fatalf("unknown TPU was described: %+v", group)
	}
}

func TestNVIDIAStampsReportNoAcceleratorKind(t *testing.T) {
	capacity, err := Measure(context.Background(), &clusterStub{nodes: twoT4Nodes, pods: `{"items":[]}`}, nil)
	if err != nil {
		t.Fatal(err)
	}
	if capacity.Accelerator() != "" || capacity.GPUs[0].Kind != "" || capacity.GPUs[0].AcceleratorType != "" {
		t.Fatalf("GPU stamp changed shape: %+v", capacity.GPUs)
	}
}
