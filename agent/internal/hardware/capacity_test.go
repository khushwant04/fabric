package hardware

import (
	"context"
	"encoding/json"
	"errors"
	"strings"
	"testing"
)

// clusterStub answers node and pod reads with canned payloads, and records the paths asked
// for, because which pods are excluded server-side is part of the contract.
type clusterStub struct {
	nodes    string
	pods     string
	nodeErr  error
	podErr   error
	nodePath string
	podPath  string
}

func (c *clusterStub) Get(_ context.Context, path string, out any) error {
	if strings.HasPrefix(path, "/api/v1/nodes") {
		c.nodePath = path
		if c.nodeErr != nil {
			return c.nodeErr
		}
		return json.Unmarshal([]byte(c.nodes), out)
	}
	c.podPath = path
	if c.podErr != nil {
		return c.podErr
	}
	return json.Unmarshal([]byte(c.pods), out)
}

const twoT4Nodes = `{"items":[
	{"metadata":{"name":"gpu-0","labels":{"nvidia.com/gpu.product":"Tesla-T4"}},
	 "status":{"allocatable":{"nvidia.com/gpu":"2"}}},
	{"metadata":{"name":"gpu-1","labels":{"nvidia.com/gpu.product":"Tesla-T4"}},
	 "status":{"allocatable":{"nvidia.com/gpu":"1"}}}]}`

func TestMeasuredCapacityIsWhatTheNodesAdvertise(t *testing.T) {
	// The number the control plane places against. Before this it was a Helm value a human
	// typed once, defaulting to zero.
	cluster := &clusterStub{nodes: twoT4Nodes, pods: `{"items":[]}`}

	capacity, err := Measure(context.Background(), cluster, nil)
	if err != nil {
		t.Fatalf("measure: %v", err)
	}
	if !capacity.Measured || !capacity.PodsMeasured {
		t.Fatalf("measurement not marked complete: %+v", capacity)
	}
	if capacity.AllocatableGPUs != 3 {
		t.Fatalf("allocatable = %d, want 3", capacity.AllocatableGPUs)
	}
	// A single replica cannot be split across nodes, so the largest node is its own fact.
	if capacity.MaxGPUsPerNode != 2 {
		t.Fatalf("max per node = %d, want 2", capacity.MaxGPUsPerNode)
	}
	if len(capacity.GPUs) != 1 {
		t.Fatalf("expected one device class, got %+v", capacity.GPUs)
	}
	group := capacity.GPUs[0]
	if group.Product != "Tesla T4" || group.Count != 3 {
		t.Fatalf("unexpected group: %+v", group)
	}
	if group.ComputeCapability != "7.5" {
		t.Fatalf("capability = %q, want 7.5", group.ComputeCapability)
	}
	// Reported in bytes because that is the unit the capability schema uses; the node
	// labels are in MiB.
	if want := int64(16384) * mibToBytes; group.MemoryBytes != want {
		t.Fatalf("memory = %d, want %d", group.MemoryBytes, want)
	}
}

func TestAGroupCollapsesToItsWeakestNode(t *testing.T) {
	// Two nodes labelled the same product with different memory is a cluster whose labels
	// disagree. A placement admitted on the larger number fails on the smaller node.
	cluster := &clusterStub{pods: `{"items":[]}`, nodes: `{"items":[
		{"metadata":{"name":"a","labels":{"nvidia.com/gpu.product":"NVIDIA-A100",
			"nvidia.com/gpu.memory":"81920",
			"nvidia.com/cuda.compute-capability.major":"8",
			"nvidia.com/cuda.compute-capability.minor":"6"}},
		 "status":{"allocatable":{"nvidia.com/gpu":"1"}}},
		{"metadata":{"name":"b","labels":{"nvidia.com/gpu.product":"NVIDIA-A100",
			"nvidia.com/gpu.memory":"40960",
			"nvidia.com/cuda.compute-capability.major":"8",
			"nvidia.com/cuda.compute-capability.minor":"0"}},
		 "status":{"allocatable":{"nvidia.com/gpu":"1"}}}]}`}

	capacity, err := Measure(context.Background(), cluster, nil)
	if err != nil {
		t.Fatalf("measure: %v", err)
	}
	if len(capacity.GPUs) != 1 {
		t.Fatalf("expected one group, got %+v", capacity.GPUs)
	}
	if want := int64(40960) * mibToBytes; capacity.GPUs[0].MemoryBytes != want {
		t.Fatalf("memory = %d, want the smaller %d", capacity.GPUs[0].MemoryBytes, want)
	}
	if capacity.GPUs[0].ComputeCapability != "8.0" {
		t.Fatalf("capability = %q, want the weaker 8.0", capacity.GPUs[0].ComputeCapability)
	}
}

func TestOneUndescribedNodeMakesItsGroupUndescribed(t *testing.T) {
	// A host may land on the node nobody could identify, so admitting on the others'
	// numbers would be admitting on luck. The control plane refuses to judge a class it
	// cannot see, which is why this has to be reported as unknown rather than omitted.
	cluster := &clusterStub{pods: `{"items":[]}`, nodes: `{"items":[
		{"metadata":{"name":"a","labels":{"nvidia.com/gpu.product":"Tesla-T4"}},
		 "status":{"allocatable":{"nvidia.com/gpu":"1"}}},
		{"metadata":{"name":"b","labels":{"nvidia.com/gpu.product":"Tesla-T4",
			"nvidia.com/gpu.memory":"0"}},
		 "status":{"allocatable":{"nvidia.com/gpu":"1"}}}]}`}

	capacity, _ := Measure(context.Background(), cluster, nil)

	if len(capacity.GPUs) != 1 || capacity.GPUs[0].Count != 2 {
		t.Fatalf("unexpected groups: %+v", capacity.GPUs)
	}
	// The label said 0, which the profiler treats as absent, so the table's 16384 stands.
	if capacity.GPUs[0].MemoryBytes == 0 {
		t.Fatal("a zero memory label should not erase the table's value")
	}
}

func TestAnUnidentifiedDeviceIsReportedAsUnknown(t *testing.T) {
	cluster := &clusterStub{pods: `{"items":[]}`, nodes: `{"items":[
		{"metadata":{"name":"a","labels":{"node.kubernetes.io/instance-type":"some-vm"}},
		 "status":{"allocatable":{"nvidia.com/gpu":"4"}}}]}`}

	capacity, _ := Measure(context.Background(), cluster, nil)

	if len(capacity.GPUs) != 1 {
		t.Fatalf("expected one group, got %+v", capacity.GPUs)
	}
	group := capacity.GPUs[0]
	if group.Product != "unknown" {
		t.Fatalf("product = %q, want an honest \"unknown\"", group.Product)
	}
	if group.MemoryBytes != 0 || group.ComputeCapability != "" {
		t.Fatalf("invented facts about an unknown device: %+v", group)
	}
	// The count is still real, and still the number of devices that exist.
	if group.Count != 4 || capacity.AllocatableGPUs != 4 {
		t.Fatalf("count was lost: %+v", capacity)
	}
}

func TestScheduledPodClaimsAreCounted(t *testing.T) {
	cluster := &clusterStub{nodes: twoT4Nodes, pods: `{"items":[
		{"metadata":{"name":"ours","labels":{"app.kubernetes.io/managed-by":"fabric-operator"}},
		 "spec":{"nodeName":"gpu-0","containers":[
			{"resources":{"limits":{"nvidia.com/gpu":"1"}}}]}},
		{"metadata":{"name":"theirs"},
		 "spec":{"nodeName":"gpu-1","containers":[
			{"resources":{"requests":{"nvidia.com/gpu":"1"}}}]}},
		{"metadata":{"name":"unscheduled"},
		 "spec":{"containers":[{"resources":{"limits":{"nvidia.com/gpu":"8"}}}]}},
		{"metadata":{"name":"cpu-only"},
		 "spec":{"nodeName":"gpu-0","containers":[{"resources":{}}]}}]}`}

	capacity, err := Measure(context.Background(), cluster, nil)
	if err != nil {
		t.Fatalf("measure: %v", err)
	}
	if capacity.RequestedGPUs != 2 {
		t.Fatalf("requested = %d, want 2", capacity.RequestedGPUs)
	}
	// Split out so the control plane can subtract foreign use without also subtracting the
	// placements it already accounts for itself.
	if capacity.FabricRequestedGPUs != 1 {
		t.Fatalf("fabric requested = %d, want 1", capacity.FabricRequestedGPUs)
	}
	// An unscheduled pod has taken nothing from anything. Counting it would make a stamp
	// look full because somebody left an unschedulable pod behind.
	if capacity.RequestedGPUs > 2 {
		t.Fatal("an unscheduled pod was counted against capacity")
	}
	// Terminal pods are excluded by the API server, not filtered afterwards, so a node's
	// history does not accumulate against it.
	if !strings.Contains(cluster.podPath, "status.phase") {
		t.Fatalf("terminal pods are not excluded server-side: %s", cluster.podPath)
	}
}

func TestAnInitContainerDoesNotAddToTheContainerSum(t *testing.T) {
	// Kubernetes' own effective-request rule: the sum across containers, or the largest
	// single init container, whichever is greater. Summing both would double-count.
	cluster := &clusterStub{nodes: twoT4Nodes, pods: `{"items":[
		{"metadata":{"name":"p"},"spec":{"nodeName":"gpu-0",
			"initContainers":[{"resources":{"limits":{"nvidia.com/gpu":"1"}}}],
			"containers":[{"resources":{"limits":{"nvidia.com/gpu":"2"}}}]}}]}`}

	capacity, _ := Measure(context.Background(), cluster, nil)

	if capacity.RequestedGPUs != 2 {
		t.Fatalf("requested = %d, want 2", capacity.RequestedGPUs)
	}
}

func TestAnInitContainerLargerThanTheContainersDecides(t *testing.T) {
	cluster := &clusterStub{nodes: twoT4Nodes, pods: `{"items":[
		{"metadata":{"name":"p"},"spec":{"nodeName":"gpu-0",
			"initContainers":[{"resources":{"limits":{"nvidia.com/gpu":"3"}}}],
			"containers":[{"resources":{"limits":{"nvidia.com/gpu":"1"}}}]}}]}`}

	capacity, _ := Measure(context.Background(), cluster, nil)

	if capacity.RequestedGPUs != 3 {
		t.Fatalf("requested = %d, want 3", capacity.RequestedGPUs)
	}
}

func TestAnUnreadableNodeListReportsNothingMeasured(t *testing.T) {
	// The caller keeps whatever it was configured with. Reporting zero GPUs would make a
	// live stamp look unplaceable because one API call failed.
	cluster := &clusterStub{nodeErr: errors.New("forbidden")}

	capacity, err := Measure(context.Background(), cluster, nil)

	if err == nil {
		t.Fatal("a failed node read must be reported")
	}
	if capacity.Measured {
		t.Fatalf("an unreadable cluster was reported as measured: %+v", capacity)
	}
}

func TestUnreadablePodsStillReportTheHardware(t *testing.T) {
	// Less severe than losing the nodes: the control plane subtracts what it has placed
	// itself regardless, so this is the pre-measurement behaviour rather than a new risk.
	cluster := &clusterStub{nodes: twoT4Nodes, podErr: errors.New("forbidden")}

	capacity, err := Measure(context.Background(), cluster, nil)

	if err == nil {
		t.Fatal("a failed pod read must be reported")
	}
	if !capacity.Measured || capacity.PodsMeasured {
		t.Fatalf("partial measurement not marked: %+v", capacity)
	}
	if capacity.AllocatableGPUs != 3 {
		t.Fatalf("hardware was discarded with the pod failure: %+v", capacity)
	}
	if capacity.RequestedGPUs != 0 {
		t.Fatalf("claims were invented: %d", capacity.RequestedGPUs)
	}
}

func TestTheNodeSelectorReachesTheAPI(t *testing.T) {
	// Measuring nodes a host will never be placed on would report capacity this stamp
	// cannot serve from.
	cluster := &clusterStub{nodes: `{"items":[]}`, pods: `{"items":[]}`}

	if _, err := Measure(
		context.Background(), cluster, map[string]string{"accelerator": "nvidia-t4"},
	); err != nil {
		t.Fatalf("measure: %v", err)
	}
	if !strings.Contains(cluster.nodePath, "labelSelector=accelerator=nvidia-t4") {
		t.Fatalf("selector not applied: %s", cluster.nodePath)
	}
}

func TestGroupsAreSortedSoAnUnchangedClusterReportsIdentically(t *testing.T) {
	// The control plane stores the whole report on every heartbeat; a reordering would look
	// like a capability change forever.
	cluster := &clusterStub{pods: `{"items":[]}`, nodes: `{"items":[
		{"metadata":{"name":"z","labels":{"nvidia.com/gpu.product":"Tesla-T4"}},
		 "status":{"allocatable":{"nvidia.com/gpu":"1"}}},
		{"metadata":{"name":"a","labels":{"nvidia.com/gpu.product":"NVIDIA-A100"}},
		 "status":{"allocatable":{"nvidia.com/gpu":"1"}}}]}`}

	capacity, _ := Measure(context.Background(), cluster, nil)

	if len(capacity.GPUs) != 2 {
		t.Fatalf("expected two groups, got %+v", capacity.GPUs)
	}
	if capacity.GPUs[0].Product != "NVIDIA A100" || capacity.GPUs[1].Product != "Tesla T4" {
		t.Fatalf("groups are not ordered: %+v", capacity.GPUs)
	}
}
