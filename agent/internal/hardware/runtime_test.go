package hardware

import (
	"context"
	"testing"
)

func TestGPUNodesWithoutAllocatableDevicesAreCounted(t *testing.T) {
	// The bootstrap labels the node accelerator=nvidia and its product from nvidia-smi; a
	// missing device plugin then shows as a labelled node advertising no nvidia.com/gpu,
	// which is what the agent's "GPU runtime missing" hint is built on.
	cluster := &clusterStub{pods: `{"items":[]}`, nodes: `{"items":[
		{"metadata":{"name":"gpu-01","labels":{"accelerator":"nvidia",
			"nvidia.com/gpu.product":"NVIDIA-RTX-PRO-6000-Blackwell-Server-Edition"}},
		 "status":{"conditions":[{"type":"Ready","status":"True"}],"allocatable":{"cpu":"32"}}},
		{"metadata":{"name":"cpu-01","labels":{}},
		 "status":{"conditions":[{"type":"Ready","status":"True"}],"allocatable":{"cpu":"8"}}}]}`}
	capacity, err := Measure(context.Background(), cluster, nil)
	if err != nil {
		t.Fatal(err)
	}
	if capacity.AllocatableGPUs != 0 || capacity.UnequippedGPUNodes != 1 {
		t.Fatalf("capacity = %+v", capacity)
	}

	// With a GPU node selector every selected node is expected to carry GPUs.
	selected, err := Measure(context.Background(), cluster, map[string]string{"pool": "gpu"})
	if err != nil {
		t.Fatal(err)
	}
	if selected.UnequippedGPUNodes != 2 {
		t.Fatalf("selected = %+v", selected)
	}

	healthy, err := Measure(context.Background(), &clusterStub{nodes: twoT4Nodes, pods: `{"items":[]}`}, nil)
	if err != nil {
		t.Fatal(err)
	}
	if healthy.UnequippedGPUNodes != 0 {
		t.Fatalf("healthy = %+v", healthy)
	}
}
