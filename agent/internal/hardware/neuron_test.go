package hardware

import (
	"context"
	"testing"
)

func TestParseNeuronInstanceTypeCountsDevices(t *testing.T) {
	cases := []struct {
		value   string
		product string
		devices int
		hbmGiB  int
		cores   int
		logical int
		class   string
		ok      bool
	}{
		{"inf2.xlarge", "Inferentia2", 1, 32, 2, 2, "neuron-inf2", true},
		{"inf2.8xlarge", "Inferentia2", 1, 32, 2, 2, "neuron-inf2", true},
		{"inf2.24xlarge", "Inferentia2", 6, 32, 2, 2, "neuron-inf2", true},
		{"INF2.48XLARGE", "Inferentia2", 12, 32, 2, 2, "neuron-inf2", true},
		{"trn1.2xlarge", "Trainium", 1, 32, 2, 2, "neuron-trn1", true},
		{"trn1.32xlarge", "Trainium", 16, 32, 2, 2, "neuron-trn1", true},
		{"trn1n.32xlarge", "Trainium", 16, 32, 2, 2, "neuron-trn1", true},
		{"trn2.48xlarge", "Trainium2", 16, 96, 8, 4, "neuron-trn2", true},
		// A size the table does not list still names its device.
		{"inf2.96xlarge", "Inferentia2", 0, 32, 2, 2, "neuron-inf2", true},
		{"inf1.xlarge", "", 0, 0, 0, 0, "", false},
		{"g5.xlarge", "", 0, 0, 0, 0, "", false},
		{"inf2", "", 0, 0, 0, 0, "", false},
		{"", "", 0, 0, 0, 0, "", false},
	}
	for _, test := range cases {
		device, devices, ok := ParseNeuronInstanceType(test.value)
		if ok != test.ok || device.Product != test.product || devices != test.devices ||
			device.HBMMiB != test.hbmGiB*1024 || device.Cores != test.cores ||
			device.LogicalCores != test.logical || device.Class != test.class {
			t.Errorf("%q = %+v, %d, %v", test.value, device, devices, ok)
		}
	}
}

const inf2Node = `{"items":[
	{"metadata":{"name":"inf2-01","labels":{"accelerator":"neuron",
		"node.kubernetes.io/instance-type":"k3s",
		"fabric.khushwant.dev/neuron-instance-type":"inf2.xlarge"}},
	 "status":{"conditions":[{"type":"Ready","status":"True"}],
	   "allocatable":{"cpu":"4","aws.amazon.com/neuron":"1","aws.amazon.com/neuroncore":"2"}}}]}`

func TestNeuronInstanceIsMeasuredAsDevicesWithHBM(t *testing.T) {
	cluster := &clusterStub{nodes: inf2Node, pods: `{"items":[
		{"metadata":{"name":"host","labels":{"app.kubernetes.io/managed-by":"fabric-operator",
			"fabric.khushwant.dev/deployment-id":"d1"}},
		 "spec":{"nodeName":"inf2-01","containers":[{"resources":{"limits":{"aws.amazon.com/neuron":"1"}}}]},
		 "status":{"phase":"Running"}}]}`}
	capacity, err := Measure(context.Background(), cluster, map[string]string{"accelerator": "neuron"})
	if err != nil {
		t.Fatal(err)
	}
	if capacity.AllocatableGPUs != 1 || capacity.MaxGPUsPerNode != 1 || capacity.UnequippedNeuronNodes != 0 {
		t.Fatalf("capacity = %+v", capacity)
	}
	if capacity.RequestedGPUs != 1 || capacity.FabricRequestedGPUs != 1 || capacity.MaxFreeGPUsPerNode != 0 {
		t.Fatalf("neuron claims were not counted: %+v", capacity)
	}
	if len(capacity.GPUs) != 1 {
		t.Fatalf("groups = %+v", capacity.GPUs)
	}
	group := capacity.GPUs[0]
	if group.Product != "Inferentia2" || group.Kind != KindNeuron || group.AcceleratorType != "inf2.xlarge" ||
		group.MemoryBytes != 32<<30 || group.ComputeCapability != "" || group.Source != "node label" ||
		group.NeuronCores != 2 {
		t.Fatalf("group = %+v", group)
	}
	if capacity.Accelerator() != KindNeuron || !capacity.Profiles[0].IsNeuron() {
		t.Fatalf("stamp not read as Neuron: %+v", capacity)
	}
}

func TestEKSNeuronNodesAreDescribedFromTheirInstanceType(t *testing.T) {
	cluster := &clusterStub{pods: `{"items":[]}`, nodes: `{"items":[
		{"metadata":{"name":"eks-a","labels":{"node.kubernetes.io/instance-type":"trn1.32xlarge"}},
		 "status":{"conditions":[{"type":"Ready","status":"True"}],"allocatable":{"aws.amazon.com/neuron":"16"}}},
		{"metadata":{"name":"eks-b","labels":{"node.kubernetes.io/instance-type":"trn2.48xlarge"}},
		 "status":{"conditions":[{"type":"Ready","status":"True"}],"allocatable":{"aws.amazon.com/neuron":"16"}}}]}`}
	capacity, err := Measure(context.Background(), cluster, nil)
	if err != nil {
		t.Fatal(err)
	}
	if len(capacity.GPUs) != 2 || capacity.GPUs[0].Product != "Trainium" || capacity.GPUs[1].Product != "Trainium2" {
		t.Fatalf("groups = %+v", capacity.GPUs)
	}
	if capacity.GPUs[0].Source != "machine type trn1.32xlarge" || capacity.GPUs[1].MemoryBytes != 96<<30 ||
		capacity.GPUs[1].NeuronCores != 8 {
		t.Fatalf("groups = %+v", capacity.GPUs)
	}
	if capacity.Accelerator() != KindNeuron {
		t.Fatal("an all-Neuron stamp must read as Neuron")
	}
}

func TestNeuronNodeWithoutDevicePluginIsUnequipped(t *testing.T) {
	cluster := &clusterStub{pods: `{"items":[]}`, nodes: `{"items":[
		{"metadata":{"name":"inf2-01","labels":{"node.kubernetes.io/instance-type":"inf2.xlarge"}},
		 "status":{"conditions":[{"type":"Ready","status":"True"}],"allocatable":{"cpu":"4"}}}]}`}
	capacity, err := Measure(context.Background(), cluster, nil)
	if err != nil {
		t.Fatal(err)
	}
	if capacity.AllocatableGPUs != 0 || capacity.UnequippedNeuronNodes != 1 ||
		capacity.UnequippedGPUNodes != 0 || capacity.UnequippedTPUNodes != 0 {
		t.Fatalf("capacity = %+v", capacity)
	}
	if capacity.Accelerator() != KindNeuron {
		t.Fatal("a Neuron stamp missing its device plugin must still read as Neuron")
	}
}

func TestUnknownNeuronInstanceStaysUndescribed(t *testing.T) {
	cluster := &clusterStub{pods: `{"items":[]}`, nodes: `{"items":[
		{"metadata":{"name":"n-01","labels":{"fabric.khushwant.dev/neuron-instance-type":"inf9.xlarge"}},
		 "status":{"conditions":[{"type":"Ready","status":"True"}],"allocatable":{"aws.amazon.com/neuron":"1"}}}]}`}
	capacity, err := Measure(context.Background(), cluster, nil)
	if err != nil {
		t.Fatal(err)
	}
	group := capacity.GPUs[0]
	if group.Product != "unknown Neuron" || group.MemoryBytes != 0 || group.Kind != KindNeuron {
		t.Fatalf("unknown Neuron was described: %+v", group)
	}
}

func TestMixedAcceleratorKindsReadAsNoSingleKind(t *testing.T) {
	cluster := &clusterStub{pods: `{"items":[]}`, nodes: `{"items":[
		{"metadata":{"name":"inf2-01","labels":{"node.kubernetes.io/instance-type":"inf2.xlarge"}},
		 "status":{"conditions":[{"type":"Ready","status":"True"}],"allocatable":{"aws.amazon.com/neuron":"1"}}},
		{"metadata":{"name":"tpu-01","labels":{"fabric.khushwant.dev/tpu-accelerator-type":"v6e-4"}},
		 "status":{"conditions":[{"type":"Ready","status":"True"}],"allocatable":{"google.com/tpu":"4"}}}]}`}
	capacity, err := Measure(context.Background(), cluster, nil)
	if err != nil {
		t.Fatal(err)
	}
	if capacity.Accelerator() != "" {
		t.Fatalf("mixed TPU and Neuron must not read as one kind: %q", capacity.Accelerator())
	}
}

func TestNVIDIAAndTPUStampsAreUnchangedByNeuronSupport(t *testing.T) {
	capacity, err := Measure(context.Background(), &clusterStub{nodes: twoT4Nodes, pods: `{"items":[]}`}, nil)
	if err != nil {
		t.Fatal(err)
	}
	if capacity.Accelerator() != "" || capacity.GPUs[0].NeuronCores != 0 || capacity.UnequippedNeuronNodes != 0 {
		t.Fatalf("GPU stamp changed shape: %+v", capacity)
	}
	capacity, err = Measure(context.Background(), &clusterStub{nodes: tpuVMNode, pods: `{"items":[]}`}, nil)
	if err != nil {
		t.Fatal(err)
	}
	if capacity.Accelerator() != KindTPU || capacity.GPUs[0].NeuronCores != 0 {
		t.Fatalf("TPU stamp changed shape: %+v", capacity)
	}
}
