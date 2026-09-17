// Package hardware describes the GPUs a stamp actually has.
//
// It exists because two different components need the same answer for two different
// reasons, and neither should be guessing. The operator needs it because several serving
// settings are properties of the GPU rather than preferences, and getting one wrong does
// not degrade serving, it prevents it: a host asked for bfloat16 on a T4 exits before it
// listens, because compute capability 7.5 has no bfloat16 at all. The agent needs it
// because the control plane decides where a deployment can be placed, and it can only
// decide that against what the cluster reports (ADR 0013).
//
// Before this package the operator read the hardware and kept the answer in-process,
// while the agent reported a number a human typed into a Helm value at install time.
// Those are two tables of GPU facts waiting to disagree, so there is now one.
package hardware

import (
	"context"
	"fmt"
	"sort"
	"strconv"
	"strings"
)

// ComputeCapability is a GPU's CUDA compute capability, which decides what arithmetic the
// hardware can perform at all.
type ComputeCapability struct {
	Major int
	Minor int
}

func (c ComputeCapability) String() string {
	return fmt.Sprintf("%d.%d", c.Major, c.Minor)
}

// AtLeast reports whether this capability is at or above another.
func (c ComputeCapability) AtLeast(other ComputeCapability) bool {
	if c.Major != other.Major {
		return c.Major > other.Major
	}
	return c.Minor >= other.Minor
}

// Known reports whether a capability was actually learned, as opposed to left at zero
// because nothing described the device.
func (c ComputeCapability) Known() bool {
	return c.Major > 0
}

// BFloat16Minimum is Ampere. Below it the type does not exist in hardware, and a model
// server told to use it refuses to start rather than falling back.
var BFloat16Minimum = ComputeCapability{Major: 8, Minor: 0}

// Profile is what the platform knows about the GPUs on one node.
type Profile struct {
	// Node the profile describes, so a stamp with mixed hardware is not summarised into
	// one profile that fits none of it.
	Node string
	// Model as reported, for example "Tesla T4".
	Model string
	// Capability decides dtype support.
	Capability ComputeCapability
	// MemoryMiB is the per-GPU frame buffer.
	MemoryMiB int
	// Count is how many GPUs the node advertises as allocatable.
	Count int
	// Source records how this was learned, because a profile inferred from a machine type
	// deserves less trust than one measured on the device, and a reader should be able to
	// tell which they are looking at.
	Source string
}

// SupportsBFloat16 reports whether this hardware can execute bfloat16.
func (p Profile) SupportsBFloat16() bool {
	return p.Capability.AtLeast(BFloat16Minimum)
}

// knownGPUs maps a GPU model to the facts that do not vary between instances of it.
// Capability is a property of the chip, so it is known once the chip is known.
var knownGPUs = map[string]struct {
	Capability ComputeCapability
	MemoryMiB  int
}{
	"Tesla T4":                {ComputeCapability{7, 5}, 16384},
	"NVIDIA A10":              {ComputeCapability{8, 6}, 24576},
	"NVIDIA A10G":             {ComputeCapability{8, 6}, 23028},
	"NVIDIA A100":             {ComputeCapability{8, 0}, 40960},
	"NVIDIA A100 80GB":        {ComputeCapability{8, 0}, 81920},
	"NVIDIA L4":               {ComputeCapability{8, 9}, 24576},
	"NVIDIA L40S":             {ComputeCapability{8, 9}, 49152},
	"NVIDIA H100":             {ComputeCapability{9, 0}, 81920},
	"NVIDIA V100":             {ComputeCapability{7, 0}, 16384},
	"NVIDIA RTX A4000":        {ComputeCapability{8, 6}, 16384},
	"NVIDIA RTX A6000":        {ComputeCapability{8, 6}, 49152},
	"NVIDIA GeForce RTX 4090": {ComputeCapability{8, 9}, 24564},
}

// machineTypeGPUs maps a cloud machine type to the GPU it carries. Used when the cluster
// reports the machine but nothing has reported the device itself, which is the normal case
// on a cluster without GPU feature discovery installed.
//
// Covering more than one cloud matters more than it looks. An unrecognised machine leaves the
// device undescribed, and the control plane cannot judge a GPU class it cannot see, so it
// admits the placement and records that the class went unverified (ADR 0013). An Azure-only
// table therefore means every GPU node on EKS or GKE without feature discovery is placed
// against blind. These entries are the families this platform is plausibly deployed on;
// anything else still degrades to an unverified admission rather than to a wrong one.
var machineTypeGPUs = map[string]string{
	// Azure.
	"Standard_NC4as_T4_v3":      "Tesla T4",
	"Standard_NC8as_T4_v3":      "Tesla T4",
	"Standard_NC16as_T4_v3":     "Tesla T4",
	"Standard_NC64as_T4_v3":     "Tesla T4",
	"Standard_NV6ads_A10_v5":    "NVIDIA A10",
	"Standard_NV12ads_A10_v5":   "NVIDIA A10",
	"Standard_NV18ads_A10_v5":   "NVIDIA A10",
	"Standard_NV36ads_A10_v5":   "NVIDIA A10",
	"Standard_NC24ads_A100_v4":  "NVIDIA A100 80GB",
	"Standard_NC48ads_A100_v4":  "NVIDIA A100 80GB",
	"Standard_NC96ads_A100_v4":  "NVIDIA A100 80GB",
	"Standard_NC40ads_H100_v5":  "NVIDIA H100",
	"Standard_NC80adis_H100_v5": "NVIDIA H100",
	// AWS. Matched by instance family below as well, because the sizes are numerous and
	// the GPU is a property of the family.
	"p3.2xlarge":   "NVIDIA V100",
	"p4d.24xlarge": "NVIDIA A100",
	"p5.48xlarge":  "NVIDIA H100",
}

// machineFamilyGPUs maps a machine-type prefix to the GPU that family carries, for clouds
// whose instance names enumerate sizes rather than devices. Consulted only after an exact
// match fails, and longest prefix wins so "g4dn" is not shadowed by "g4".
var machineFamilyGPUs = map[string]string{
	// AWS.
	"g4dn.": "Tesla T4",
	"g5.":   "NVIDIA A10G",
	"g6.":   "NVIDIA L4",
	"g6e.":  "NVIDIA L40S",
	"p3.":   "NVIDIA V100",
	"p4d.":  "NVIDIA A100",
	"p4de.": "NVIDIA A100 80GB",
	"p5.":   "NVIDIA H100",
	// GCP, whose A- and G-series carry one device per family.
	"a2-":          "NVIDIA A100",
	"a2-ultragpu-": "NVIDIA A100 80GB",
	"a3-":          "NVIDIA H100",
	"g2-":          "NVIDIA L4",
}

// acceleratorLabelGPUs maps the accelerator labels managed Kubernetes offerings apply to
// their GPU pools. GKE sets cloud.google.com/gke-accelerator on every GPU node without any
// feature discovery installed, which makes it the cheapest way to describe a GKE stamp.
var acceleratorLabelGPUs = map[string]string{
	"nvidia-tesla-t4":       "Tesla T4",
	"nvidia-tesla-v100":     "NVIDIA V100",
	"nvidia-tesla-a100":     "NVIDIA A100",
	"nvidia-a100-80gb":      "NVIDIA A100 80GB",
	"nvidia-l4":             "NVIDIA L4",
	"nvidia-h100-80gb":      "NVIDIA H100",
	"nvidia-h100-mega-80gb": "NVIDIA H100",
}

// GPUResource is the extended resource an NVIDIA device plugin advertises.
const GPUResource = "nvidia.com/gpu"

// node is the part of a Kubernetes node this needs.
type node struct {
	Metadata struct {
		Name   string            `json:"name"`
		Labels map[string]string `json:"labels"`
	} `json:"metadata"`
	Status struct {
		Allocatable map[string]string `json:"allocatable"`
	} `json:"status"`
}

type nodeList struct {
	Items []node `json:"items"`
}

// Getter is the read this package needs, kept narrow so it can be substituted in a test.
type Getter interface {
	Get(ctx context.Context, path string, out any) error
}

// ProfileNodes returns a profile for every node advertising a GPU.
//
// Read from the cluster rather than configured, because the operator is told which nodes
// to place hosts on by selector, not by name, and the answer changes when a pool is
// scaled or replaced.
func ProfileNodes(ctx context.Context, client Getter, selector map[string]string) ([]Profile, error) {
	path := "/api/v1/nodes"
	if len(selector) > 0 {
		terms := make([]string, 0, len(selector))
		for key, value := range selector {
			terms = append(terms, key+"="+value)
		}
		sort.Strings(terms)
		path += "?labelSelector=" + strings.Join(terms, ",")
	}

	var nodes nodeList
	if err := client.Get(ctx, path, &nodes); err != nil {
		return nil, fmt.Errorf("list gpu nodes: %w", err)
	}

	profiles := make([]Profile, 0, len(nodes.Items))
	for _, item := range nodes.Items {
		count := 0
		if raw, ok := item.Status.Allocatable[GPUResource]; ok {
			count, _ = strconv.Atoi(raw)
		}
		if count == 0 {
			// No allocatable GPU means nothing can be scheduled here, whatever the
			// machine type claims. A node whose driver or device plugin is missing looks
			// exactly like this, and it is the reason a GPU pool can exist while
			// advertising nothing.
			continue
		}

		profile := Profile{Node: item.Metadata.Name, Count: count, Source: "unknown"}

		// Preferred: a label naming the device, which GPU feature discovery provides.
		switch {
		case item.Metadata.Labels["nvidia.com/gpu.product"] != "":
			profile.Model = normaliseModel(item.Metadata.Labels["nvidia.com/gpu.product"])
			profile.Source = "node label"
		default:
			// Then the provider's own accelerator label, which needs nothing installed.
			accelerator := item.Metadata.Labels["cloud.google.com/gke-accelerator"]
			if model, ok := acceleratorLabelGPUs[strings.ToLower(accelerator)]; ok {
				profile.Model = model
				profile.Source = "accelerator label " + accelerator
				break
			}
			machine := item.Metadata.Labels["node.kubernetes.io/instance-type"]
			if machine == "" {
				break
			}
			if model, ok := machineTypeGPUs[machine]; ok {
				profile.Model = model
				profile.Source = "machine type " + machine
			} else if model, family := machineFamilyGPU(machine); model != "" {
				profile.Model = model
				profile.Source = "machine family " + family
			} else {
				profile.Source = "unrecognised machine type " + machine
			}
		}

		if known, ok := knownGPUs[profile.Model]; ok {
			profile.Capability = known.Capability
			profile.MemoryMiB = known.MemoryMiB
		}
		// A label from feature discovery carries the real numbers, which beat the table.
		if raw := item.Metadata.Labels["nvidia.com/gpu.memory"]; raw != "" {
			if memory, err := strconv.Atoi(raw); err == nil && memory > 0 {
				profile.MemoryMiB = memory
			}
		}
		if raw := item.Metadata.Labels["nvidia.com/cuda.compute-capability.major"]; raw != "" {
			major, majorErr := strconv.Atoi(raw)
			minor, minorErr := strconv.Atoi(item.Metadata.Labels["nvidia.com/cuda.compute-capability.minor"])
			if majorErr == nil && minorErr == nil {
				profile.Capability = ComputeCapability{Major: major, Minor: minor}
				profile.Source = "node label"
			}
		}

		profiles = append(profiles, profile)
	}

	sort.Slice(profiles, func(i, j int) bool { return profiles[i].Node < profiles[j].Node })
	return profiles, nil
}

func normaliseModel(label string) string {
	// Feature discovery uses hyphens where the device reports spaces.
	return strings.ReplaceAll(label, "-", " ")
}

// machineFamilyGPU resolves a machine type by its family prefix, returning the device and the
// prefix that matched so the profile can record how it was learned.
//
// Longest prefix wins, so "g4dn.xlarge" resolves as g4dn rather than being shadowed by a
// shorter entry, and "a2-ultragpu-1g" resolves to the 80 GB part rather than the 40 GB one.
func machineFamilyGPU(machine string) (model string, family string) {
	lowered := strings.ToLower(machine)
	for prefix, candidate := range machineFamilyGPUs {
		if !strings.HasPrefix(lowered, prefix) {
			continue
		}
		if len(prefix) > len(family) {
			model, family = candidate, prefix
		}
	}
	return model, family
}
