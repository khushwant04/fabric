package agent

import (
	"context"
	"errors"
	"fmt"
	"testing"
	"time"

	"github.com/khushwant04/fabric/agent/internal/controlplane"
	"github.com/khushwant04/fabric/agent/internal/hardware"
	"github.com/khushwant04/fabric/agent/internal/state"
)

// measuredT4Stamp is a plausible small stamp: three T4s across two nodes, one of them
// already held by a workload Fabric did not place.
func measuredT4Stamp() hardware.Capacity {
	return hardware.Capacity{
		Measured:     true,
		PodsMeasured: true,
		GPUs: []hardware.GPUGroup{
			{
				Product:           "Tesla T4",
				Count:             3,
				MemoryBytes:       16106127360,
				ComputeCapability: "7.5",
			},
		},
		AllocatableGPUs:     3,
		MaxGPUsPerNode:      2,
		MaxFreeGPUsPerNode:  2,
		AvailableGPUSlots:   []int{2, 1, 0, 0, 0, 0, 0, 0},
		RequestedGPUs:       1,
		FabricRequestedGPUs: 0,
	}
}

// countingSource records how often it was asked and what to answer with.
type countingSource struct {
	capacity hardware.Capacity
	err      error
	calls    int
}

func (c *countingSource) Measure(context.Context) (hardware.Capacity, error) {
	c.calls++
	return c.capacity, c.err
}

func TestMeasuredCapacityIsWhatTheStampReports(t *testing.T) {
	// The whole point of M4: before this the agent copied a startup-time struct on every
	// heartbeat, so gpus was always empty and requested_gpus always zero, and the control
	// plane placed against numbers nobody was measuring.
	stub := &controlPlaneStub{}
	server := stub.server(t)
	instance, _ := newAgent(t, server.URL)
	instance.config.Capabilities = controlplane.Capabilities{
		Orchestrator:    "k3s",
		Region:          "us-west",
		AgentVersion:    "test",
		AllocatableGPUs: 1,
	}
	instance.config.Capacity = &countingSource{capacity: measuredT4Stamp()}

	if err := instance.Ensure(context.Background()); err != nil {
		t.Fatalf("ensure: %v", err)
	}
	if _, err := instance.ReconcileOnce(context.Background()); err != nil {
		t.Fatalf("reconcile: %v", err)
	}

	reported := stub.lastCapabilities
	if reported == nil {
		t.Fatal("no capabilities were reported")
	}
	if reported.AllocatableGPUs != 3 {
		t.Fatalf("allocatable = %d; the configured 1 was not replaced", reported.AllocatableGPUs)
	}
	if reported.MaxGPUsPerNode != 2 {
		t.Fatalf("max per node = %d, want 2", reported.MaxGPUsPerNode)
	}
	if reported.MaxFreeGPUsPerNode != 2 {
		t.Fatalf("max free per node = %d, want 2", reported.MaxFreeGPUsPerNode)
	}
	if len(reported.AvailableGPUSlots) != 8 || reported.AvailableGPUSlots[1] != 1 {
		t.Fatalf("available slots = %v, want one 2-GPU replica", reported.AvailableGPUSlots)
	}
	if reported.RequestedGPUs != 1 || reported.FabricRequestedGPUs != 0 {
		t.Fatalf("claims not carried: %+v", reported)
	}
	if len(reported.GPUs) != 1 || reported.GPUs[0].ComputeCapability != "7.5" {
		t.Fatalf("device class not carried: %+v", reported.GPUs)
	}
	// Facts the cluster cannot answer still come from configuration; a measurement replaces
	// only the GPU numbers.
	if reported.Orchestrator != "k3s" || reported.Region != "us-west" {
		t.Fatalf("configured identity was dropped: %+v", reported)
	}
	if reported.AgentVersion != "test" {
		t.Fatalf("agent version was dropped: %+v", reported)
	}
}

func TestCapacityIsMeasuredBeforeEnrollment(t *testing.T) {
	// Enrollment carries the first capability report, so a stamp measured only afterwards
	// would be unplaceable until its first heartbeat landed.
	stub := &controlPlaneStub{}
	server := stub.server(t)
	instance, _ := newAgent(t, server.URL)
	instance.config.Capacity = &countingSource{capacity: measuredT4Stamp()}

	if err := instance.Ensure(context.Background()); err != nil {
		t.Fatalf("ensure: %v", err)
	}

	if stub.enrolledCapabilities == nil {
		t.Fatal("enrollment carried no capabilities")
	}
	if stub.enrolledCapabilities.AllocatableGPUs != 3 {
		t.Fatalf("enrollment reported %d GPUs, not the measured 3",
			stub.enrolledCapabilities.AllocatableGPUs)
	}
}

func TestAnUnmeasurableClusterFallsBackToTheConfiguredCount(t *testing.T) {
	// A missing permission must degrade to the declared number, not to a stamp that appears
	// to have no hardware: refusing to report would make every placement fail.
	stub := &controlPlaneStub{}
	server := stub.server(t)
	instance, _ := newAgent(t, server.URL)
	instance.config.Capabilities.AllocatableGPUs = 2
	instance.config.Capacity = &countingSource{err: errors.New("forbidden")}

	if err := instance.Ensure(context.Background()); err != nil {
		t.Fatalf("ensure: %v", err)
	}
	if _, err := instance.ReconcileOnce(context.Background()); err != nil {
		t.Fatalf("reconcile: %v", err)
	}

	if stub.lastCapabilities.AllocatableGPUs != 2 {
		t.Fatalf("allocatable = %d, want the configured 2",
			stub.lastCapabilities.AllocatableGPUs)
	}
	// Never nil: the control-plane schema declares gpus a list, so a null would be a
	// contract violation rather than an empty stamp.
	if stub.lastCapabilities.GPUs == nil {
		t.Fatal("gpus was reported as null rather than an empty list")
	}
}

func TestALostMeasurementKeepsTheLastKnownHardware(t *testing.T) {
	// One timed-out node list must not make a live stamp look unplaceable.
	stub := &controlPlaneStub{}
	server := stub.server(t)
	instance, _ := newAgent(t, server.URL)
	source := &countingSource{capacity: measuredT4Stamp()}
	instance.config.Capacity = source
	// Re-measure every pass, so the second pass genuinely tries and fails.
	instance.config.CapacityInterval = time.Nanosecond

	if err := instance.Ensure(context.Background()); err != nil {
		t.Fatalf("ensure: %v", err)
	}
	if _, err := instance.ReconcileOnce(context.Background()); err != nil {
		t.Fatalf("first reconcile: %v", err)
	}

	source.capacity = hardware.Capacity{}
	source.err = errors.New("timeout")
	if _, err := instance.ReconcileOnce(context.Background()); err != nil {
		t.Fatalf("second reconcile: %v", err)
	}

	if stub.lastCapabilities.AllocatableGPUs != 3 {
		t.Fatalf("allocatable = %d; the last known measurement was discarded",
			stub.lastCapabilities.AllocatableGPUs)
	}
}

func TestTheClusterIsNotRemeasuredOnEveryPass(t *testing.T) {
	// Node hardware does not move, and a read per poll is a cluster-wide pod list every
	// fifteen seconds for an answer that has not changed.
	stub := &controlPlaneStub{desired: []controlplane.DesiredState{
		{StampID: stampID}, {StampID: stampID}, {StampID: stampID},
	}}
	server := stub.server(t)
	instance, _ := newAgent(t, server.URL)
	source := &countingSource{capacity: measuredT4Stamp()}
	instance.config.Capacity = source
	instance.config.CapacityInterval = time.Hour

	if err := instance.Ensure(context.Background()); err != nil {
		t.Fatalf("ensure: %v", err)
	}
	for pass := 0; pass < 3; pass++ {
		if _, err := instance.ReconcileOnce(context.Background()); err != nil {
			t.Fatalf("pass %d: %v", pass, err)
		}
	}

	// Once at enrollment, and not again inside the interval.
	if source.calls != 1 {
		t.Fatalf("measured %d times across enrollment and three passes", source.calls)
	}
}

func TestNoCapacitySourceReportsConfigurationUnchanged(t *testing.T) {
	// A stamp with no Kubernetes access at all, which is the file-publishing shape.
	stub := &controlPlaneStub{}
	server := stub.server(t)
	instance, _ := newAgent(t, server.URL)
	instance.config.Capabilities.AllocatableGPUs = 7

	if err := instance.Ensure(context.Background()); err != nil {
		t.Fatalf("ensure: %v", err)
	}
	if _, err := instance.ReconcileOnce(context.Background()); err != nil {
		t.Fatalf("reconcile: %v", err)
	}

	if stub.lastCapabilities.AllocatableGPUs != 7 {
		t.Fatalf("allocatable = %d, want the configured 7",
			stub.lastCapabilities.AllocatableGPUs)
	}
}

func TestGPUCountTravelsFromTheSpec(t *testing.T) {
	// The control plane admits a placement against replicas x gpu_count. If that number
	// does not reach the workload, the check governs nothing.
	spec := map[string]any{
		"runtime":   map[string]any{"release": "r1"},
		"replicas":  float64(2),
		"resources": map[string]any{"gpu_count": float64(4), "gpu_class": "a100"},
	}
	if got := gpuCountFromSpec(spec); got != 4 {
		t.Fatalf("gpu count = %d, want 4", got)
	}
}

func TestAnAbsentGPUCountMeansTheStampsOwnSetting(t *testing.T) {
	// A deployment declared before the field existed must be unchanged, not sized to zero.
	cases := []map[string]any{
		{},
		{"resources": map[string]any{}},
		{"resources": map[string]any{"gpu_count": float64(0)}},
		{"resources": map[string]any{"gpu_count": "four"}},
		{"resources": "not-an-object"},
	}
	for index, spec := range cases {
		if got := gpuCountFromSpec(spec); got != 0 {
			t.Fatalf("case %d: gpu count = %d, want 0", index, got)
		}
	}
}

func TestGPUCountReachesTheDeclaredDeployment(t *testing.T) {
	stub := &controlPlaneStub{desired: []controlplane.DesiredState{{
		StampID:       stampID,
		MaxGeneration: 1,
		Deployments: []controlplane.DesiredDeployment{{
			DeploymentID:      deployA,
			AccountID:         customerA,
			ModelAlias:        "alias",
			DesiredGeneration: 1,
			Spec: map[string]any{
				"runtime":   map[string]any{"release": "r1"},
				"replicas":  float64(2),
				"resources": map[string]any{"gpu_count": float64(2)},
			},
		}},
	}}}
	server := stub.server(t)
	instance, _ := newAgent(t, server.URL)

	if err := instance.Ensure(context.Background()); err != nil {
		t.Fatalf("ensure: %v", err)
	}
	configured, err := instance.ReconcileOnce(context.Background())
	if err != nil {
		t.Fatalf("reconcile: %v", err)
	}

	if len(configured) != 1 {
		t.Fatalf("expected one deployment, got %d", len(configured))
	}
	if configured[0].GPUCount != 2 {
		t.Fatalf("gpu count = %d, want 2", configured[0].GPUCount)
	}
	if configured[0].Replicas != 2 {
		t.Fatalf("replicas = %d, want 2", configured[0].Replicas)
	}
}

func TestAnUnmeasuredClaimCountIsReportedAsSuch(t *testing.T) {
	// Zero claimed GPUs looks exactly like an idle cluster, and the control plane decides
	// placements on the difference. Logging it locally is not enough.
	stub := &controlPlaneStub{}
	server := stub.server(t)
	instance, _ := newAgent(t, server.URL)
	partial := measuredT4Stamp()
	partial.PodsMeasured = false
	partial.RequestedGPUs = 0
	partial.FabricRequestedGPUs = 0
	partial.AvailableGPUSlots = nil
	partial.PodClaimsError = "pods is forbidden"
	instance.config.Capacity = &countingSource{capacity: partial}

	if err := instance.Ensure(context.Background()); err != nil {
		t.Fatalf("ensure: %v", err)
	}
	if _, err := instance.ReconcileOnce(context.Background()); err != nil {
		t.Fatalf("reconcile: %v", err)
	}

	if stub.lastCapabilities.AvailableGPUSlots == nil {
		t.Fatal("an unavailable packing measurement was reported as null rather than an empty list")
	}
	if len(stub.lastCapabilities.AvailableGPUSlots) != 0 {
		t.Fatalf("unmeasured slots = %v, want an empty list", stub.lastCapabilities.AvailableGPUSlots)
	}
	if stub.lastCapabilities.GPUClaimsMeasured {
		t.Fatal("an unmeasured claim count was reported as measured")
	}
	// The hardware is still reported: only the claims were missing.
	if stub.lastCapabilities.AllocatableGPUs != 3 {
		t.Fatalf("hardware was dropped: %+v", stub.lastCapabilities)
	}
}

func TestAMeasuredClaimCountSaysSo(t *testing.T) {
	stub := &controlPlaneStub{}
	server := stub.server(t)
	instance, _ := newAgent(t, server.URL)
	instance.config.Capacity = &countingSource{capacity: measuredT4Stamp()}

	if err := instance.Ensure(context.Background()); err != nil {
		t.Fatalf("ensure: %v", err)
	}
	if _, err := instance.ReconcileOnce(context.Background()); err != nil {
		t.Fatalf("reconcile: %v", err)
	}

	if !stub.lastCapabilities.GPUClaimsMeasured {
		t.Fatal("a measured claim count was reported as unmeasured")
	}
}

func TestPerDeploymentClaimsReachTheControlPlane(t *testing.T) {
	// The comparison the control plane makes is per deployment, so the breakdown has to survive
	// the trip rather than being collapsed into the total on the way.
	stub := &controlPlaneStub{}
	server := stub.server(t)
	instance, _ := newAgent(t, server.URL)
	capacity := measuredT4Stamp()
	capacity.FabricRequestedGPUs = 3
	capacity.RequestedGPUs = 3
	capacity.FabricClaims = []hardware.DeploymentClaim{
		{DeploymentID: deployA, GPUs: 2},
		{DeploymentID: deployB, GPUs: 1},
	}
	instance.config.Capacity = &countingSource{capacity: capacity}

	if err := instance.Ensure(context.Background()); err != nil {
		t.Fatalf("ensure: %v", err)
	}
	if _, err := instance.ReconcileOnce(context.Background()); err != nil {
		t.Fatalf("reconcile: %v", err)
	}

	claims := stub.lastCapabilities.FabricGPUClaims
	if len(claims) != 2 {
		t.Fatalf("claims = %+v, want two entries", claims)
	}
	if claims[0].DeploymentID != deployA || claims[0].GPUs != 2 {
		t.Fatalf("first claim = %+v", claims[0])
	}
	if claims[1].DeploymentID != deployB || claims[1].GPUs != 1 {
		t.Fatalf("second claim = %+v", claims[1])
	}
}

func TestClaimsAreNeverReportedAsNull(t *testing.T) {
	// The control plane declares the field a list. A stamp that measured nothing reports an
	// empty one rather than a null, which would be a contract violation rather than an idle
	// cluster.
	stub := &controlPlaneStub{}
	server := stub.server(t)
	instance, _ := newAgent(t, server.URL)

	if err := instance.Ensure(context.Background()); err != nil {
		t.Fatalf("ensure: %v", err)
	}
	if _, err := instance.ReconcileOnce(context.Background()); err != nil {
		t.Fatalf("reconcile: %v", err)
	}

	if stub.lastCapabilities.FabricGPUClaims == nil {
		t.Fatal("claims were reported as null")
	}
}

func TestLiveClaimsWinTheBoundOverTerminatingAndOutOfBandPods(t *testing.T) {
	// A terminating pod still holds a GPU and stays in the physical total, but it must not
	// displace one of the 512 supported live commitment ids. If it did, the omitted live id
	// would look row-ahead forever and every later multi-GPU admission would wait forever.
	instance := New(Config{}, discardLogger())
	instance.measured = hardware.Capacity{
		Measured:            true,
		PodsMeasured:        true,
		AllocatableGPUs:     hardware.MaxReportedClaims + 1,
		RequestedGPUs:       hardware.MaxReportedClaims + 1,
		FabricRequestedGPUs: hardware.MaxReportedClaims + 1,
		FabricClaims: []hardware.DeploymentClaim{
			// Larger than every live claim, so measurement order alone would keep it first.
			{DeploymentID: "terminating-or-out-of-band", GPUs: 1},
		},
	}
	for index := 0; index < hardware.MaxReportedClaims; index++ {
		id := fmt.Sprintf("live-%03d", index)
		instance.known[id] = state.Deployment{DeploymentID: id}
		instance.measured.FabricClaims = append(
			instance.measured.FabricClaims,
			hardware.DeploymentClaim{DeploymentID: id, GPUs: 1},
		)
	}

	reported := instance.reportedCapabilities()

	if len(reported.FabricGPUClaims) != hardware.MaxReportedClaims {
		t.Fatalf("reported %d claims, want %d",
			len(reported.FabricGPUClaims), hardware.MaxReportedClaims)
	}
	seen := make(map[string]bool, len(reported.FabricGPUClaims))
	for _, claim := range reported.FabricGPUClaims {
		seen[claim.DeploymentID] = true
	}
	for deploymentID := range instance.known {
		if !seen[deploymentID] {
			t.Fatalf("live claim %q was displaced", deploymentID)
		}
	}
	if seen["terminating-or-out-of-band"] {
		t.Fatal("unsupported claim displaced a live assignment")
	}
	// The omitted pod remains in the total and is charged as untracked by the receiver.
	if reported.FabricRequestedGPUs != hardware.MaxReportedClaims+1 {
		t.Fatalf("fabric total = %d, want %d",
			reported.FabricRequestedGPUs, hardware.MaxReportedClaims+1)
	}
}
