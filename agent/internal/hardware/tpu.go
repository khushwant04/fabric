package hardware

import (
	"strconv"
	"strings"
)

// Cloud TPU support.
//
// A TPU VM has no NVIDIA driver and no device plugin. Its chips appear to the host as
// /dev/accel* (v4, v5e) or /dev/vfio/* (v5p, v6e), and the VM's metadata names the slice,
// for example "v6e-4". The agent runs in an unprivileged pod and can see neither, so the
// detection happens where both are visible: the bootstrap reads the metadata and the device
// nodes on the host, labels the node with the accelerator type, and advertises the chips as
// the google.com/tpu extended resource (the name GKE's TPU device plugin uses, so a GKE TPU
// pool is described by the same code). From there a TPU node is measured exactly like a GPU
// node: allocatable devices from the node, claims from pod requests.

// TPUResource is the extended resource TPU chips are advertised as.
const TPUResource = "google.com/tpu"

// KindTPU marks a profile or group as Cloud TPU chips. GPU profiles leave the kind empty,
// which keeps everything an NVIDIA stamp reports exactly as it was.
const KindTPU = "tpu"

// VendorGoogle is the vendor reported for TPU chips.
const VendorGoogle = "google"

// TPUAcceleratorTypeLabel is set by the bootstrap on a TPU VM's node to the metadata's
// accelerator-type, for example "v6e-4" or "v5litepod-4".
const TPUAcceleratorTypeLabel = "fabric.khushwant.dev/tpu-accelerator-type"

// gkeTPUAcceleratorLabel is what GKE sets on TPU node pools.
const gkeTPUAcceleratorLabel = "cloud.google.com/gke-tpu-accelerator"

// TPUChip is what does not vary between chips of one TPU generation.
type TPUChip struct {
	// Generation as Google names it in accelerator types, for example "v6e".
	Generation string
	// Product is how the chip is described to people and in the capability report.
	Product string
	// HBMMiB is the high-bandwidth memory on one chip.
	HBMMiB int
	// CoresPerChip is how many TensorCores the accelerator-type suffix counts per chip.
	// v4 and v5p types count TensorCores (v5p-8 is four chips); v5e and v6e count chips.
	CoresPerChip int
	// Class is the placement class a deployment names to ask for this chip.
	Class string
}

// knownTPUs lists the generations vLLM serves on. Older generations (v2, v3) are left out on
// purpose: an unknown type stays undescribed rather than being placed against.
var knownTPUs = map[string]TPUChip{
	"v4":  {Generation: "v4", Product: "TPU v4", HBMMiB: 32 * 1024, CoresPerChip: 2, Class: "tpu-v4"},
	"v5e": {Generation: "v5e", Product: "TPU v5e", HBMMiB: 16 * 1024, CoresPerChip: 1, Class: "tpu-v5e"},
	"v5p": {Generation: "v5p", Product: "TPU v5p", HBMMiB: 95 * 1024, CoresPerChip: 2, Class: "tpu-v5p"},
	"v6e": {Generation: "v6e", Product: "TPU v6e", HBMMiB: 32 * 1024, CoresPerChip: 1, Class: "tpu-v6e"},
}

// tpuTypeAliases maps the prefixes accelerator types use to a generation. v5e is spelled
// "v5litepod" by the TPU VM API.
var tpuTypeAliases = map[string]string{
	"v4": "v4", "v5litepod": "v5e", "v5e": "v5e", "v5p": "v5p", "v6e": "v6e",
}

// gkeTPUAccelerators maps GKE's TPU node-pool label to a generation.
var gkeTPUAccelerators = map[string]string{
	"tpu-v4-podslice":      "v4",
	"tpu-v5-lite-podslice": "v5e",
	"tpu-v5-lite-device":   "v5e",
	"tpu-v5p-slice":        "v5p",
	"tpu-v6e-slice":        "v6e",
}

// tpuMachineFamilies maps GCE TPU machine-type prefixes to a generation, for a node that
// reports only its instance type.
var tpuMachineFamilies = map[string]string{
	"ct4p-":  "v4",
	"ct5lp-": "v5e",
	"ct5l-":  "v5e",
	"ct5p-":  "v5p",
	"ct6e-":  "v6e",
}

// LookupTPU returns the chip facts for a generation.
func LookupTPU(generation string) (TPUChip, bool) {
	chip, ok := knownTPUs[strings.ToLower(strings.TrimSpace(generation))]
	return chip, ok
}

// ParseAcceleratorType reads a TPU accelerator type such as "v6e-4", "v5litepod-4" or
// "v5p-8", returning the chip and how many chips the slice has.
func ParseAcceleratorType(value string) (TPUChip, int, bool) {
	value = strings.ToLower(strings.TrimSpace(value))
	prefix, suffix, found := strings.Cut(value, "-")
	if !found {
		return TPUChip{}, 0, false
	}
	generation, ok := tpuTypeAliases[prefix]
	if !ok {
		return TPUChip{}, 0, false
	}
	chip := knownTPUs[generation]
	cores, err := strconv.Atoi(suffix)
	if err != nil || cores <= 0 {
		return TPUChip{}, 0, false
	}
	chips := cores / chip.CoresPerChip
	if chips == 0 {
		chips = 1
	}
	return chip, chips, true
}

// tpuLabelled reports whether a node is labelled as carrying TPU chips, whatever it
// advertises. Such a node with no allocatable google.com/tpu has lost the advertisement (a
// kubelet re-registration resets extended resources) or never had it.
func tpuLabelled(labels map[string]string) bool {
	return labels[TPUAcceleratorTypeLabel] != "" ||
		labels[gkeTPUAcceleratorLabel] != "" ||
		labels["accelerator"] == "tpu"
}

// describeTPU fills in a TPU profile from the node's labels, most specific first.
func describeTPU(profile *Profile, labels map[string]string) {
	profile.Kind = KindTPU
	var chip TPUChip
	var known bool
	switch {
	case labels[TPUAcceleratorTypeLabel] != "":
		accelerator := labels[TPUAcceleratorTypeLabel]
		chip, _, known = ParseAcceleratorType(accelerator)
		profile.AcceleratorType = strings.ToLower(accelerator)
		profile.Source = "node label"
		if !known {
			profile.Source = "unrecognised TPU accelerator type " + accelerator
		}
	case labels[gkeTPUAcceleratorLabel] != "":
		accelerator := labels[gkeTPUAcceleratorLabel]
		chip, known = knownTPUs[gkeTPUAccelerators[strings.ToLower(accelerator)]]
		profile.Source = "accelerator label " + accelerator
		if !known {
			profile.Source = "unrecognised TPU accelerator label " + accelerator
		}
	default:
		machine := strings.ToLower(labels["node.kubernetes.io/instance-type"])
		for prefix, generation := range tpuMachineFamilies {
			if machine != "" && strings.HasPrefix(machine, prefix) {
				chip, known = knownTPUs[generation], true
				profile.Source = "machine family " + prefix
				break
			}
		}
		if !known && machine != "" {
			profile.Source = "unrecognised machine type " + machine
		}
	}
	if known {
		profile.Model = chip.Product
		profile.MemoryMiB = chip.HBMMiB
	}
}
