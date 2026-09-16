package operator

import (
	"context"
	"encoding/json"
	"testing"
)

// fakeGetter answers a node list read.
type fakeGetter struct {
	payload  string
	lastPath string
}

func (f *fakeGetter) Get(_ context.Context, path string, out any) error {
	f.lastPath = path
	return json.Unmarshal([]byte(f.payload), out)
}

func TestAMachineTypeIsEnoughToKnowTheGPU(t *testing.T) {
	// A cluster without GPU feature discovery reports the machine but nothing about the
	// device. The machine type is still enough: compute capability is a property of the
	// chip, and the chip is implied by the SKU.
	getter := &fakeGetter{payload: `{"items":[{
		"metadata":{"name":"gpu-0","labels":{"node.kubernetes.io/instance-type":"Standard_NC4as_T4_v3"}},
		"status":{"allocatable":{"nvidia.com/gpu":"1"}}}]}`}

	profiles, err := ProfileGPUNodes(context.Background(), getter, map[string]string{"gpu": "t4"})
	if err != nil {
		t.Fatalf("profile: %v", err)
	}
	if len(profiles) != 1 {
		t.Fatalf("expected one profile, got %d", len(profiles))
	}
	if profiles[0].Model != "Tesla T4" || profiles[0].Capability.String() != "7.5" {
		t.Fatalf("unexpected profile: %+v", profiles[0])
	}
	if profiles[0].SupportsBFloat16() {
		t.Fatal("a T4 was reported as supporting bfloat16")
	}
	// The selector has to reach the API, or every node in the cluster is profiled.
	if want := "labelSelector=gpu=t4"; !contains(getter.lastPath, want) {
		t.Fatalf("selector not applied: %s", getter.lastPath)
	}
}

func TestANodeAdvertisingNoGPUIsNotProfiled(t *testing.T) {
	// A pool whose driver or device plugin is missing looks exactly like this, and it is
	// why a GPU pool can exist while advertising nothing schedulable.
	getter := &fakeGetter{payload: `{"items":[{
		"metadata":{"name":"gpu-0","labels":{"node.kubernetes.io/instance-type":"Standard_NC4as_T4_v3"}},
		"status":{"allocatable":{"cpu":"4"}}}]}`}

	profiles, err := ProfileGPUNodes(context.Background(), getter, nil)
	if err != nil {
		t.Fatalf("profile: %v", err)
	}
	if len(profiles) != 0 {
		t.Fatalf("expected no profiles, got %+v", profiles)
	}
}

func TestDeviceLabelsBeatTheTable(t *testing.T) {
	getter := &fakeGetter{payload: `{"items":[{
		"metadata":{"name":"gpu-0","labels":{
			"nvidia.com/gpu.product":"NVIDIA-A100",
			"nvidia.com/cuda.compute-capability.major":"8",
			"nvidia.com/cuda.compute-capability.minor":"0",
			"nvidia.com/gpu.memory":"81920"}},
		"status":{"allocatable":{"nvidia.com/gpu":"2"}}}]}`}

	profiles, _ := ProfileGPUNodes(context.Background(), getter, nil)
	if len(profiles) != 1 || profiles[0].MemoryMiB != 81920 || profiles[0].Count != 2 {
		t.Fatalf("labels were not preferred: %+v", profiles)
	}
	if !profiles[0].SupportsBFloat16() {
		t.Fatal("an A100 was reported as lacking bfloat16")
	}
	if profiles[0].Source != "node label" {
		t.Fatalf("source should record that this was measured, got %q", profiles[0].Source)
	}
}

func TestBFloat16IsRefusedOnHardwareThatCannotRunIt(t *testing.T) {
	// This is the failure that motivated profiling: the chart's default was bfloat16, the
	// hardware was a T4, and the host exited before it ever listened.
	host := testHost()
	host.DType = "bfloat16"
	profiles := []GPUProfile{{Node: "gpu-0", Model: "Tesla T4", Capability: ComputeCapability{Major: 7, Minor: 5}, MemoryMiB: 16384, Count: 1}}

	adjusted, changes := applyProfile(host, profiles)

	if adjusted.DType != "float16" {
		t.Fatalf("dtype was left at %q", adjusted.DType)
	}
	if len(changes) != 1 || changes[0].Setting != "dtype" {
		t.Fatalf("the change was not recorded: %+v", changes)
	}
	// The reason has to name the hardware, or an operator cannot tell why their value moved.
	if !contains(changes[0].Reason, "7.5") {
		t.Fatalf("reason does not explain itself: %q", changes[0].Reason)
	}
}

func TestASupportedRequestIsLeftAlone(t *testing.T) {
	// Only settings that cannot work are changed. Overriding a merely suboptimal choice
	// would make the platform unpredictable.
	host := testHost()
	host.DType = "bfloat16"
	profiles := []GPUProfile{{Node: "gpu-0", Model: "NVIDIA A100", Capability: ComputeCapability{Major: 8, Minor: 0}, MemoryMiB: 40960, Count: 1}}

	adjusted, changes := applyProfile(host, profiles)

	if adjusted.DType != "bfloat16" || len(changes) != 0 {
		t.Fatalf("a supported request was altered: %q %+v", adjusted.DType, changes)
	}
}

func TestTheWeakestGPUDecides(t *testing.T) {
	// A host may be scheduled onto any GPU node in the pool, so a setting that only works
	// on the best of them fails intermittently, which is worse than failing always.
	host := testHost()
	host.DType = "bfloat16"
	profiles := []GPUProfile{
		{Node: "gpu-0", Model: "NVIDIA A100", Capability: ComputeCapability{Major: 8, Minor: 0}, MemoryMiB: 40960, Count: 1},
		{Node: "gpu-1", Model: "Tesla T4", Capability: ComputeCapability{Major: 7, Minor: 5}, MemoryMiB: 16384, Count: 1},
	}

	adjusted, changes := applyProfile(host, profiles)

	if adjusted.DType != "float16" || len(changes) != 1 {
		t.Fatalf("the weakest GPU did not decide: %q %+v", adjusted.DType, changes)
	}
}

func contains(haystack, needle string) bool {
	return len(haystack) >= len(needle) && (haystack == needle || indexOf(haystack, needle) >= 0)
}

func indexOf(haystack, needle string) int {
	for i := 0; i+len(needle) <= len(haystack); i++ {
		if haystack[i:i+len(needle)] == needle {
			return i
		}
	}
	return -1
}

func TestASmallDeviceGetsItsMemoryFractionLowered(t *testing.T) {
	// gpu-memory-utilization is a fraction of *total* memory, but the CUDA context, driver
	// allocations and fragmentation live outside that accounting. On a large card the
	// omission never shows; on a small one the same fraction leaves less than the runtime
	// needs and the server dies allocating rather than starting. This is what the profiled
	// smallest-GPU memory is for: before M4 it was computed and thrown away.
	host := testHost()
	host.DType = "float16"
	host.GPUMemoryUtilization = "0.97"
	profiles := []GPUProfile{{
		Node: "gpu-0", Model: "Small", Capability: ComputeCapability{Major: 8, Minor: 0},
		MemoryMiB: 8192, Count: 1,
	}}

	adjusted, changes := applyProfile(host, profiles)

	if len(changes) != 1 || changes[0].Setting != "gpu-memory-utilization" {
		t.Fatalf("the change was not recorded: %+v", changes)
	}
	// (8192 - 1024) / 8192 = 0.875, floored to two decimals so the result never rises back
	// above the ceiling it came from.
	if adjusted.GPUMemoryUtilization != "0.87" {
		t.Fatalf("utilization = %q, want 0.87", adjusted.GPUMemoryUtilization)
	}
	// The reason has to name the hardware, or an operator cannot tell why their value moved.
	if !contains(changes[0].Reason, "8192") {
		t.Fatalf("reason does not explain itself: %q", changes[0].Reason)
	}
}

func TestAWorkableMemoryFractionIsLeftAlone(t *testing.T) {
	// Only settings that cannot work are changed. A T4 at 0.80 leaves over 3 GiB spare.
	host := testHost()
	host.DType = "float16"
	profiles := []GPUProfile{{
		Node: "gpu-0", Model: "Tesla T4", Capability: ComputeCapability{Major: 7, Minor: 5},
		MemoryMiB: 16384, Count: 1,
	}}

	adjusted, changes := applyProfile(host, profiles)

	if adjusted.GPUMemoryUtilization != host.GPUMemoryUtilization || len(changes) != 0 {
		t.Fatalf("a workable fraction was altered: %q %+v", adjusted.GPUMemoryUtilization, changes)
	}
}

func TestTheSmallestGPUDecidesTheMemoryFraction(t *testing.T) {
	// A host may land on either node, so a fraction that only fits the larger one fails
	// intermittently.
	host := testHost()
	host.DType = "float16"
	host.GPUMemoryUtilization = "0.95"
	profiles := []GPUProfile{
		{Node: "big", Model: "NVIDIA A100", Capability: ComputeCapability{Major: 8, Minor: 0},
			MemoryMiB: 40960, Count: 1},
		{Node: "small", Model: "Tesla T4", Capability: ComputeCapability{Major: 7, Minor: 5},
			MemoryMiB: 16384, Count: 1},
	}

	adjusted, changes := applyProfile(host, profiles)

	// (16384 - 1024) / 16384 = 0.9375 -> 0.93.
	if adjusted.GPUMemoryUtilization != "0.93" {
		t.Fatalf("utilization = %q, want 0.93 from the T4", adjusted.GPUMemoryUtilization)
	}
	if len(changes) != 1 {
		t.Fatalf("expected one change, got %+v", changes)
	}
}

func TestADeviceTooSmallToServeIsNotGivenAnInventedFraction(t *testing.T) {
	// No fraction makes this host work. Replacing a clear allocation failure with a
	// confusing one helps nobody, and the setting is not what is wrong.
	host := testHost()
	host.DType = "float16"
	host.GPUMemoryUtilization = "0.99"
	profiles := []GPUProfile{{
		Node: "tiny", Model: "Tiny", Capability: ComputeCapability{Major: 8, Minor: 0},
		MemoryMiB: 1536, Count: 1,
	}}

	adjusted, changes := applyProfile(host, profiles)

	if adjusted.GPUMemoryUtilization != "0.99" || len(changes) != 0 {
		t.Fatalf("a value was invented for an unusable device: %q %+v",
			adjusted.GPUMemoryUtilization, changes)
	}
}

func TestAnUnparseableFractionIsLeftForTheServerToReject(t *testing.T) {
	// Not this code's error to report: the server names the real problem.
	host := testHost()
	host.DType = "float16"
	host.GPUMemoryUtilization = "eighty-five percent"
	profiles := []GPUProfile{{
		Node: "gpu-0", Model: "Small", Capability: ComputeCapability{Major: 8, Minor: 0},
		MemoryMiB: 8192, Count: 1,
	}}

	adjusted, changes := applyProfile(host, profiles)

	if adjusted.GPUMemoryUtilization != "eighty-five percent" || len(changes) != 0 {
		t.Fatalf("an unparseable value was rewritten: %q %+v",
			adjusted.GPUMemoryUtilization, changes)
	}
}

func TestAnUndescribedNodeSuppressesTheMemoryClamp(t *testing.T) {
	// A host may land on the node nobody could describe, so a clamp computed from the nodes
	// that *were* described would be sized for hardware the pod may never see. The reported
	// capacity demotes a group the same way; this keeps the two consistent.
	host := testHost()
	host.DType = "float16"
	host.GPUMemoryUtilization = "0.99"
	profiles := []GPUProfile{
		{Node: "known", Model: "Small", Capability: ComputeCapability{Major: 8, Minor: 0},
			MemoryMiB: 8192, Count: 1},
		{Node: "unknown", Model: "", Capability: ComputeCapability{Major: 8, Minor: 0},
			MemoryMiB: 0, Count: 1},
	}

	adjusted, changes := applyProfile(host, profiles)

	if adjusted.GPUMemoryUtilization != "0.99" || len(changes) != 0 {
		t.Fatalf("clamped against partially described hardware: %q %+v",
			adjusted.GPUMemoryUtilization, changes)
	}
}
