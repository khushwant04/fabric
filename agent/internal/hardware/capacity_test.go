package hardware

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
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
	// Less severe than losing the nodes: the hardware is known and only the claims are
	// missing. A non-nil error is reserved for a result nobody can use, so that a future
	// caller writing the ordinary `if err != nil { return }` does not throw away a good node
	// measurement because a pod list was refused.
	cluster := &clusterStub{nodes: twoT4Nodes, podErr: errors.New("forbidden")}

	capacity, err := Measure(context.Background(), cluster, nil)

	if err != nil {
		t.Fatalf("a usable measurement was returned as an error: %v", err)
	}
	if !capacity.Measured || capacity.PodsMeasured {
		t.Fatalf("partial measurement not marked: %+v", capacity)
	}
	// The reason still has to reach the caller, or the degradation is silent.
	if !strings.Contains(capacity.PodClaimsError, "forbidden") {
		t.Fatalf("pod failure not explained: %q", capacity.PodClaimsError)
	}
	if capacity.AllocatableGPUs != 3 {
		t.Fatalf("hardware was discarded with the pod failure: %+v", capacity)
	}
	if capacity.RequestedGPUs != 0 {
		t.Fatalf("claims were invented: %d", capacity.RequestedGPUs)
	}
}

func TestClaimsAreCountedOnlyOnTheProfiledNodes(t *testing.T) {
	// The selector scopes what this stamp offers. Counting claims from the rest of the
	// cluster subtracts GPUs from a total that never included them, which on a shared
	// cluster goes negative and refuses every placement with a reason pointing at capacity
	// rather than at configuration.
	cluster := &clusterStub{nodes: twoT4Nodes, pods: `{"items":[
		{"metadata":{"name":"ours"},"spec":{"nodeName":"gpu-0","containers":[
			{"resources":{"limits":{"nvidia.com/gpu":"1"}}}]}},
		{"metadata":{"name":"neighbours-training-job"},
		 "spec":{"nodeName":"other-pool-7","containers":[
			{"resources":{"limits":{"nvidia.com/gpu":"64"}}}]}}]}`}

	capacity, err := Measure(
		context.Background(), cluster, map[string]string{"pool": "fabric"},
	)
	if err != nil {
		t.Fatalf("measure: %v", err)
	}

	if capacity.RequestedGPUs != 1 {
		t.Fatalf("requested = %d, want only the claim on a profiled node",
			capacity.RequestedGPUs)
	}
	if capacity.AllocatableGPUs-capacity.RequestedGPUs < 0 {
		t.Fatal("free capacity went negative from a claim outside this stamp")
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

func TestGPUsAreDescribedWithoutFeatureDiscoveryOnEveryCloudWeSupport(t *testing.T) {
	// An undescribed device is not refused, it is admitted with the GPU class unjudged
	// (ADR 0013). An Azure-only machine table therefore meant every GPU node on EKS or GKE
	// without feature discovery was placed against blind, which is a much larger hole than
	// "an older agent".
	cases := []struct {
		name   string
		labels string
		model  string
	}{
		{"gke accelerator label", `"cloud.google.com/gke-accelerator":"nvidia-l4"`, "NVIDIA L4"},
		{"aws g4dn family", `"node.kubernetes.io/instance-type":"g4dn.2xlarge"`, "Tesla T4"},
		{"aws g5 family", `"node.kubernetes.io/instance-type":"g5.12xlarge"`, "NVIDIA A10G"},
		{"aws p4d exact", `"node.kubernetes.io/instance-type":"p4d.24xlarge"`, "NVIDIA A100"},
		{"gcp a2 family", `"node.kubernetes.io/instance-type":"a2-highgpu-1g"`, "NVIDIA A100"},
		{
			"gcp a2 ultra takes the longer prefix",
			`"node.kubernetes.io/instance-type":"a2-ultragpu-1g"`,
			"NVIDIA A100 80GB",
		},
		{"azure still works", `"node.kubernetes.io/instance-type":"Standard_NC4as_T4_v3"`, "Tesla T4"},
	}

	for _, testCase := range cases {
		t.Run(testCase.name, func(t *testing.T) {
			cluster := &clusterStub{pods: `{"items":[]}`, nodes: `{"items":[{"metadata":{
				"name":"gpu-0","labels":{` + testCase.labels + `}},
				"status":{"allocatable":{"nvidia.com/gpu":"1"}}}]}`}

			capacity, err := Measure(context.Background(), cluster, nil)
			if err != nil {
				t.Fatalf("measure: %v", err)
			}
			if len(capacity.GPUs) != 1 || capacity.GPUs[0].Product != testCase.model {
				t.Fatalf("product = %+v, want %q", capacity.GPUs, testCase.model)
			}
			// Described means the control plane can judge the class, which needs both.
			if capacity.GPUs[0].MemoryBytes == 0 || capacity.GPUs[0].ComputeCapability == "" {
				t.Fatalf("device is not fully described: %+v", capacity.GPUs[0])
			}
			// Provenance has to say this was inferred, not measured on the device.
			if capacity.Profiles[0].Source == "node label" {
				t.Fatalf("an inferred profile claims to be measured: %+v", capacity.Profiles[0])
			}
		})
	}
}

func TestAnUnrecognisedMachineIsStillReportedAsUnknown(t *testing.T) {
	// The table can only ever be partial, so the honest failure has to stay honest.
	cluster := &clusterStub{pods: `{"items":[]}`, nodes: `{"items":[{"metadata":{
		"name":"gpu-0","labels":{"node.kubernetes.io/instance-type":"zz9-plural-z-alpha"}},
		"status":{"allocatable":{"nvidia.com/gpu":"1"}}}]}`}

	capacity, _ := Measure(context.Background(), cluster, nil)

	if capacity.GPUs[0].Product != "unknown" || capacity.GPUs[0].ComputeCapability != "" {
		t.Fatalf("an unknown machine was described anyway: %+v", capacity.GPUs[0])
	}
	if !strings.Contains(capacity.Profiles[0].Source, "unrecognised") {
		t.Fatalf("provenance does not say it failed: %q", capacity.Profiles[0].Source)
	}
}

func TestFabricClaimsAreReportedPerDeployment(t *testing.T) {
	// The control plane compares each deployment's running pods against what it committed for
	// that deployment. A stamp-wide total cannot support that: one deployment running ahead of
	// its rows and another lagging behind them are indistinguishable in a sum, and the two
	// errors cancel into an overcommitment.
	cluster := &clusterStub{nodes: twoT4Nodes, pods: `{"items":[
		{"metadata":{"name":"a-1","labels":{
			"app.kubernetes.io/managed-by":"fabric-operator",
			"fabric.khushwant.dev/deployment-id":"dep-a"}},
		 "spec":{"nodeName":"gpu-0","containers":[
			{"resources":{"limits":{"nvidia.com/gpu":"1"}}}]}},
		{"metadata":{"name":"a-2","labels":{
			"app.kubernetes.io/managed-by":"fabric-operator",
			"fabric.khushwant.dev/deployment-id":"dep-a"}},
		 "spec":{"nodeName":"gpu-0","containers":[
			{"resources":{"limits":{"nvidia.com/gpu":"1"}}}]}},
		{"metadata":{"name":"b-1","labels":{
			"app.kubernetes.io/managed-by":"fabric-operator",
			"fabric.khushwant.dev/deployment-id":"dep-b"}},
		 "spec":{"nodeName":"gpu-1","containers":[
			{"resources":{"limits":{"nvidia.com/gpu":"1"}}}]}},
		{"metadata":{"name":"theirs"},
		 "spec":{"nodeName":"gpu-1","containers":[
			{"resources":{"limits":{"nvidia.com/gpu":"1"}}}]}}]}`}

	capacity, err := Measure(context.Background(), cluster, nil)
	if err != nil {
		t.Fatalf("measure: %v", err)
	}

	if capacity.RequestedGPUs != 4 || capacity.FabricRequestedGPUs != 3 {
		t.Fatalf("totals wrong: %+v", capacity)
	}
	// Ordered largest first, so a truncated report keeps the claims that matter most.
	want := []DeploymentClaim{{DeploymentID: "dep-a", GPUs: 2}, {DeploymentID: "dep-b", GPUs: 1}}
	if len(capacity.FabricClaims) != len(want) {
		t.Fatalf("claims = %+v, want %+v", capacity.FabricClaims, want)
	}
	for index, claim := range capacity.FabricClaims {
		if claim != want[index] {
			t.Fatalf("claim %d = %+v, want %+v", index, claim, want[index])
		}
	}
}

func TestAFabricPodWithNoDeploymentLabelStillCounts(t *testing.T) {
	// It is holding a device either way. Counting it only in the total makes the control plane
	// charge it as untracked, which is conservative; dropping it would offer the device twice.
	cluster := &clusterStub{nodes: twoT4Nodes, pods: `{"items":[
		{"metadata":{"name":"orphan","labels":{
			"app.kubernetes.io/managed-by":"fabric-operator"}},
		 "spec":{"nodeName":"gpu-0","containers":[
			{"resources":{"limits":{"nvidia.com/gpu":"2"}}}]}}]}`}

	capacity, _ := Measure(context.Background(), cluster, nil)

	if capacity.FabricRequestedGPUs != 2 {
		t.Fatalf("fabric total = %d, want 2", capacity.FabricRequestedGPUs)
	}
	if len(capacity.FabricClaims) != 0 {
		t.Fatalf("an unlabelled pod was attributed to a deployment: %+v", capacity.FabricClaims)
	}
}

func TestMeasurementRetainsClaimsForAgentPrioritization(t *testing.T) {
	// Hardware sees terminating and out-of-band pods too. It must retain them all in-process
	// so the agent can put every live assignment first before applying the bounded wire cap;
	// truncating here can permanently evict a supported commitment id.
	items := make([]string, 0, MaxReportedClaims+10)
	for index := 0; index < MaxReportedClaims+10; index++ {
		items = append(items, fmt.Sprintf(`{"metadata":{"name":"p%d","labels":{
			"app.kubernetes.io/managed-by":"fabric-operator",
			"fabric.khushwant.dev/deployment-id":"dep-%03d"}},
			"spec":{"nodeName":"gpu-0","containers":[
				{"resources":{"limits":{"nvidia.com/gpu":"1"}}}]}}`, index, index))
	}
	cluster := &clusterStub{
		nodes: twoT4Nodes,
		pods:  `{"items":[` + strings.Join(items, ",") + `]}`,
	}

	capacity, _ := Measure(context.Background(), cluster, nil)

	if len(capacity.FabricClaims) != MaxReportedClaims+10 {
		t.Fatalf("claims = %d, want every measured claim", len(capacity.FabricClaims))
	}
	// The total still accounts for every device, so nothing goes missing.
	if capacity.FabricRequestedGPUs != MaxReportedClaims+10 {
		t.Fatalf("fabric total = %d, want %d",
			capacity.FabricRequestedGPUs, MaxReportedClaims+10)
	}
}

func TestLargestFreeNodeIsReportedSeparatelyFromLargestNode(t *testing.T) {
	// Two half-used two-device nodes have two devices free in total and no node that can
	// schedule a two-device replica. A stamp-wide free count plus the allocatable width
	// would admit it and Kubernetes would leave it Pending forever.
	cluster := &clusterStub{nodes: `{"items":[
		{"metadata":{"name":"gpu-0","labels":{"nvidia.com/gpu.product":"Tesla-T4"}},
		 "status":{"allocatable":{"nvidia.com/gpu":"2"}}},
		{"metadata":{"name":"gpu-1","labels":{"nvidia.com/gpu.product":"Tesla-T4"}},
		 "status":{"allocatable":{"nvidia.com/gpu":"2"}}}]}`,
		pods: `{"items":[
			{"metadata":{"name":"busy-0"},"spec":{"nodeName":"gpu-0","containers":[
				{"resources":{"limits":{"nvidia.com/gpu":"1"}}}]}},
			{"metadata":{"name":"busy-1"},"spec":{"nodeName":"gpu-1","containers":[
				{"resources":{"limits":{"nvidia.com/gpu":"1"}}}]}}]}`}

	capacity, err := Measure(context.Background(), cluster, nil)
	if err != nil {
		t.Fatalf("measure: %v", err)
	}
	if capacity.MaxGPUsPerNode != 2 {
		t.Fatalf("max allocatable per node = %d, want 2", capacity.MaxGPUsPerNode)
	}
	if capacity.MaxFreeGPUsPerNode != 1 {
		t.Fatalf("max free per node = %d, want 1", capacity.MaxFreeGPUsPerNode)
	}
	if got := capacity.AvailableGPUSlots[1]; got != 0 {
		t.Fatalf("2-GPU slots = %d, want 0", got)
	}
	if capacity.AllocatableGPUs-capacity.RequestedGPUs != 2 {
		t.Fatalf("stamp-wide free = %d, want 2",
			capacity.AllocatableGPUs-capacity.RequestedGPUs)
	}
}

func TestNoFreeNodeIsReportedAsZero(t *testing.T) {
	// Zero is not "unreported" when claims were measured; it means no new GPU pod fits.
	cluster := &clusterStub{nodes: `{"items":[
		{"metadata":{"name":"gpu-0","labels":{"nvidia.com/gpu.product":"Tesla-T4"}},
		 "status":{"allocatable":{"nvidia.com/gpu":"1"}}}]}`,
		pods: `{"items":[
			{"metadata":{"name":"busy"},"spec":{"nodeName":"gpu-0","containers":[
				{"resources":{"limits":{"nvidia.com/gpu":"1"}}}]}}]}`}

	capacity, err := Measure(context.Background(), cluster, nil)
	if err != nil {
		t.Fatalf("measure: %v", err)
	}
	if !capacity.PodsMeasured {
		t.Fatal("claims were not marked measured")
	}
	if capacity.MaxFreeGPUsPerNode != 0 {
		t.Fatalf("max free per node = %d, want 0", capacity.MaxFreeGPUsPerNode)
	}
}

func TestSlotsAccountForSeveralReplicasNotOnlyTheLargestFreeNode(t *testing.T) {
	// Free nodes [3,1] have four GPUs in total and a largest node of three, but can host only
	// one 2-GPU pod. A total plus max-free check would admit two replicas and strand the second.
	cluster := &clusterStub{nodes: `{"items":[
		{"metadata":{"name":"gpu-0","labels":{"nvidia.com/gpu.product":"Tesla-T4"}},
		 "status":{"allocatable":{"nvidia.com/gpu":"4"}}},
		{"metadata":{"name":"gpu-1","labels":{"nvidia.com/gpu.product":"Tesla-T4"}},
		 "status":{"allocatable":{"nvidia.com/gpu":"2"}}}]}`,
		pods: `{"items":[
			{"metadata":{"name":"busy-0"},"spec":{"nodeName":"gpu-0","containers":[
				{"resources":{"limits":{"nvidia.com/gpu":"1"}}}]}},
			{"metadata":{"name":"busy-1"},"spec":{"nodeName":"gpu-1","containers":[
				{"resources":{"limits":{"nvidia.com/gpu":"1"}}}]}}]}`}

	capacity, err := Measure(context.Background(), cluster, nil)
	if err != nil {
		t.Fatalf("measure: %v", err)
	}
	if capacity.MaxFreeGPUsPerNode != 3 {
		t.Fatalf("max free per node = %d, want 3", capacity.MaxFreeGPUsPerNode)
	}
	if got := capacity.AvailableGPUSlots[1]; got != 1 {
		t.Fatalf("2-GPU slots = %d, want 1", got)
	}
}
