package hardware

import (
	"strings"
)

// AWS Neuron support (Inferentia2 and Trainium).
//
// An inf2, trn1 or trn2 instance has no NVIDIA driver. Its accelerators appear to the host as
// /dev/neuron0..N, driven by the aws-neuronx-dkms driver that the Neuron Deep Learning AMI
// ships, and the Neuron device plugin advertises them to Kubernetes as the
// aws.amazon.com/neuron (devices) and aws.amazon.com/neuroncore (cores) extended resources.
// Fabric counts and requests whole devices: a device's HBM is the unit a model's weights are
// sized against, and one replica owning whole devices keeps the device plugin's core
// allocation contiguous. vLLM's tensor parallelism counts NeuronCores, so the operator turns
// devices into cores when it renders the host.
//
// On EKS the node's instance-type label names the instance. On a k3s instance the bootstrap
// reads it from EC2 instance metadata and labels the node with it, because k3s reports its own
// instance type ("k3s").

// NeuronResource is the extended resource Neuron devices are advertised as.
const NeuronResource = "aws.amazon.com/neuron"

// NeuronCoreResource is the extended resource the device plugin advertises cores as. Fabric
// does not request it; it is named so a reader can tell the two apart.
const NeuronCoreResource = "aws.amazon.com/neuroncore"

// KindNeuron marks a profile or group as AWS Neuron devices.
const KindNeuron = "neuron"

// VendorAWS is the vendor reported for Neuron devices.
const VendorAWS = "aws"

// NeuronInstanceTypeLabel is set by the bootstrap on a Neuron instance's node to the EC2
// instance type, for example "inf2.xlarge" or "trn1.32xlarge".
const NeuronInstanceTypeLabel = "fabric.khushwant.dev/neuron-instance-type"

// NeuronDevice is what does not vary between devices of one Neuron generation.
type NeuronDevice struct {
	// Generation as AWS names the instance family, for example "inf2".
	Generation string
	// Product is how the device is described to people and in the capability report.
	Product string
	// HBMMiB is the high-bandwidth memory on one device.
	HBMMiB int
	// Cores is the physical NeuronCores on one device.
	Cores int
	// LogicalCores is how many NeuronCores the runtime exposes per device at its default
	// logical-core setting, which is what vLLM's tensor parallelism counts. Trainium2
	// groups two physical cores into one logical core by default (LNC=2).
	LogicalCores int
	// Class is the placement class a deployment names to ask for this device.
	Class string
}

// knownNeuron lists the generations vLLM serves on. Inferentia1 is left out on purpose: it
// has no vLLM backend, so an inf1 node stays undescribed rather than placed against.
var knownNeuron = map[string]NeuronDevice{
	"inf2": {Generation: "inf2", Product: "Inferentia2", HBMMiB: 32 * 1024, Cores: 2, LogicalCores: 2, Class: "neuron-inf2"},
	"trn1": {Generation: "trn1", Product: "Trainium", HBMMiB: 32 * 1024, Cores: 2, LogicalCores: 2, Class: "neuron-trn1"},
	"trn2": {Generation: "trn2", Product: "Trainium2", HBMMiB: 96 * 1024, Cores: 8, LogicalCores: 4, Class: "neuron-trn2"},
}

// neuronFamilies maps an EC2 instance family to its generation. trn1n is trn1 with more
// network bandwidth.
var neuronFamilies = map[string]string{
	"inf2": "inf2", "trn1": "trn1", "trn1n": "trn1", "trn2": "trn2", "trn2u": "trn2",
}

// neuronInstanceDevices is how many devices each instance size carries.
var neuronInstanceDevices = map[string]int{
	"inf2.xlarge": 1, "inf2.8xlarge": 1, "inf2.24xlarge": 6, "inf2.48xlarge": 12,
	"trn1.2xlarge": 1, "trn1.32xlarge": 16, "trn1n.32xlarge": 16,
	"trn2.3xlarge": 1, "trn2.48xlarge": 16, "trn2u.48xlarge": 16,
}

// LookupNeuron returns the device facts for a generation.
func LookupNeuron(generation string) (NeuronDevice, bool) {
	device, ok := knownNeuron[strings.ToLower(strings.TrimSpace(generation))]
	return device, ok
}

// ParseNeuronInstanceType reads an EC2 instance type such as "inf2.xlarge", returning the
// device and how many devices that size carries (zero when the size is not in the table but
// the family is).
func ParseNeuronInstanceType(value string) (NeuronDevice, int, bool) {
	value = strings.ToLower(strings.TrimSpace(value))
	family, size, found := strings.Cut(value, ".")
	if !found || size == "" {
		return NeuronDevice{}, 0, false
	}
	generation, ok := neuronFamilies[family]
	if !ok {
		return NeuronDevice{}, 0, false
	}
	return knownNeuron[generation], neuronInstanceDevices[value], true
}

// isNeuronInstanceType reports whether an instance type belongs to a Neuron family, whether
// or not Fabric serves on it (inf1 counts: it is still a Neuron node, not a GPU node).
func isNeuronInstanceType(value string) bool {
	family, _, found := strings.Cut(strings.ToLower(strings.TrimSpace(value)), ".")
	if !found {
		return false
	}
	_, ok := neuronFamilies[family]
	return ok || family == "inf1"
}

// neuronLabelled reports whether a node is labelled as carrying Neuron devices, whatever it
// advertises. Such a node with no allocatable aws.amazon.com/neuron is missing the Neuron
// device plugin or the driver.
func neuronLabelled(labels map[string]string) bool {
	return labels[NeuronInstanceTypeLabel] != "" ||
		labels["accelerator"] == "neuron" ||
		isNeuronInstanceType(labels["node.kubernetes.io/instance-type"])
}

// describeNeuron fills in a Neuron profile from the node's labels, most specific first.
func describeNeuron(profile *Profile, labels map[string]string) {
	profile.Kind = KindNeuron
	instance := labels[NeuronInstanceTypeLabel]
	source := "node label"
	if instance == "" {
		instance = labels["node.kubernetes.io/instance-type"]
		source = "machine type " + instance
	}
	if instance == "" {
		return
	}
	device, _, known := ParseNeuronInstanceType(instance)
	profile.AcceleratorType = strings.ToLower(instance)
	profile.Source = source
	if !known {
		profile.Source = "unrecognised Neuron instance type " + instance
		return
	}
	profile.Model = device.Product
	profile.MemoryMiB = device.HBMMiB
	profile.NeuronCores = device.Cores
}
