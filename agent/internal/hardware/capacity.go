package hardware

import (
	"context"
	"errors"
	"fmt"
	"sort"
	"strconv"
	"strings"
)

// mibToBytes converts the unit node labels report into the unit the control plane's
// capability schema uses.
const mibToBytes int64 = 1 << 20

// FabricManagedByLabel marks a pod the operator created. It is how a GPU request Fabric
// itself placed is told apart from one belonging to somebody else's workload, which
// matters because the control plane accounts for Fabric's own placements from its own
// records and would otherwise subtract them twice (ADR 0013).
const FabricManagedByLabel = "fabric-operator"

// GPUGroup is one class of device on a stamp, aggregated across the nodes carrying it.
//
// Aggregated rather than per node because the control plane decides placement, not
// scheduling, and node names are not its business: the capability report is deliberately
// bounded to facts about capacity.
type GPUGroup struct {
	Product           string
	Count             int
	MemoryBytes       int64
	ComputeCapability string
}

// Capacity is what one stamp can offer, as measured.
type Capacity struct {
	// Profiles is the per-node detail, kept for the operator's settings decisions and for
	// logging. It does not leave the cluster.
	Profiles []Profile
	// GPUs is the aggregated view the control plane receives.
	GPUs []GPUGroup
	// AllocatableGPUs is every schedulable GPU on the profiled nodes.
	AllocatableGPUs int
	// MaxGPUsPerNode bounds what a single replica can ask for. A stamp-wide total cannot
	// answer whether one pod wanting four GPUs can be scheduled at all.
	MaxGPUsPerNode int
	// RequestedGPUs is what scheduled pods have already claimed, all workloads included.
	RequestedGPUs int
	// FabricRequestedGPUs is the subset claimed by model hosts this platform created.
	FabricRequestedGPUs int
	// Measured reports that the nodes were read. False means nothing here is trustworthy
	// and the caller should keep whatever it was configured with.
	Measured bool
	// PodsMeasured reports that pod claims were read. False leaves RequestedGPUs at zero,
	// which overstates free capacity by however much a foreign workload is using.
	PodsMeasured bool
}

// pod is the part of a Kubernetes pod this needs.
type pod struct {
	Metadata struct {
		Name      string            `json:"name"`
		Namespace string            `json:"namespace"`
		Labels    map[string]string `json:"labels"`
	} `json:"metadata"`
	Spec struct {
		NodeName       string      `json:"nodeName"`
		Containers     []container `json:"containers"`
		InitContainers []container `json:"initContainers"`
	} `json:"spec"`
	Status struct {
		Phase string `json:"phase"`
	} `json:"status"`
}

type container struct {
	Resources struct {
		Limits   map[string]string `json:"limits"`
		Requests map[string]string `json:"requests"`
	} `json:"resources"`
}

// gpus returns the device count this container claims.
//
// The limit is read first because Kubernetes requires request and limit to be equal for an
// extended resource, and a manifest that sets only one of them commonly sets the limit.
func (c container) gpus() int {
	if raw, ok := c.Resources.Limits[GPUResource]; ok {
		if count, err := strconv.Atoi(raw); err == nil {
			return count
		}
	}
	if raw, ok := c.Resources.Requests[GPUResource]; ok {
		if count, err := strconv.Atoi(raw); err == nil {
			return count
		}
	}
	return 0
}

// gpus returns the devices this pod holds, by Kubernetes' effective-request rule: the sum
// across ordinary containers, or the largest single init container, whichever is greater.
func (p pod) gpus() int {
	total := 0
	for _, c := range p.Spec.Containers {
		total += c.gpus()
	}
	initial := 0
	for _, c := range p.Spec.InitContainers {
		if count := c.gpus(); count > initial {
			initial = count
		}
	}
	if initial > total {
		return initial
	}
	return total
}

type podList struct {
	Items []pod `json:"items"`
}

// MeasurePodGPURequests sums the devices scheduled pods have claimed, returning the total
// and the subset belonging to Fabric's own model hosts.
//
// Only scheduled pods count. A pod with no node has not taken a device from anything, and
// counting it would make a stamp look full because somebody left an unschedulable pod
// behind. The cost is that a foreign pod about to be scheduled is invisible for a moment.
//
// Terminal pods are excluded server-side, so a node's history does not accumulate against
// its capacity.
func MeasurePodGPURequests(ctx context.Context, client Getter) (total int, fabric int, err error) {
	path := "/api/v1/pods?fieldSelector=" +
		"status.phase%21%3DSucceeded,status.phase%21%3DFailed"
	var pods podList
	if err := client.Get(ctx, path, &pods); err != nil {
		return 0, 0, fmt.Errorf("list gpu pods: %w", err)
	}

	for _, item := range pods.Items {
		if item.Spec.NodeName == "" {
			continue
		}
		count := item.gpus()
		if count == 0 {
			continue
		}
		total += count
		if item.Metadata.Labels["app.kubernetes.io/managed-by"] == FabricManagedByLabel {
			fabric += count
		}
	}
	return total, fabric, nil
}

// Measure reads what this cluster can offer.
//
// A node-read failure leaves Measured false, which tells the caller to keep the capacity
// it was configured with rather than report zero: a missing permission should degrade to
// the declared number, not to a stamp that appears to have no hardware. A pod-read failure
// is reported the same way through PodsMeasured, and is less severe because the control
// plane subtracts what it has placed itself regardless.
//
// Both failures are also returned as an error so the caller can log why, which is why the
// returned Capacity is still meaningful alongside a non-nil error.
func Measure(ctx context.Context, client Getter, selector map[string]string) (Capacity, error) {
	var problems []error
	capacity := Capacity{}

	profiles, err := ProfileNodes(ctx, client, selector)
	if err != nil {
		return capacity, err
	}
	capacity.Measured = true
	capacity.Profiles = profiles
	capacity.GPUs = groupProfiles(profiles)
	for _, profile := range profiles {
		capacity.AllocatableGPUs += profile.Count
		if profile.Count > capacity.MaxGPUsPerNode {
			capacity.MaxGPUsPerNode = profile.Count
		}
	}

	total, fabric, podErr := MeasurePodGPURequests(ctx, client)
	if podErr != nil {
		problems = append(problems, podErr)
	} else {
		capacity.PodsMeasured = true
		capacity.RequestedGPUs = total
		capacity.FabricRequestedGPUs = fabric
	}

	return capacity, errors.Join(problems...)
}

// aggregate accumulates one device class across the nodes carrying it.
//
// Memory and capability collapse to the weakest node in the group, and a single node that
// nothing described makes the whole group undescribed. Both rules exist because a host may
// be scheduled onto any node in the group: a placement admitted on the strongest node's
// numbers is a placement that fails on the others, which is worse than being refused.
type aggregate struct {
	count           int
	memoryMiB       int
	memoryKnown     bool
	capability      ComputeCapability
	capabilityKnown bool
}

func (a *aggregate) add(profile Profile) {
	a.count += profile.Count

	if profile.MemoryMiB <= 0 {
		a.memoryKnown = false
		a.memoryMiB = 0
	} else if a.memoryKnown && (a.memoryMiB == 0 || profile.MemoryMiB < a.memoryMiB) {
		a.memoryMiB = profile.MemoryMiB
	}

	if !profile.Capability.Known() {
		a.capabilityKnown = false
		a.capability = ComputeCapability{}
	} else if a.capabilityKnown &&
		(!a.capability.Known() || a.capability.AtLeast(profile.Capability)) {
		a.capability = profile.Capability
	}
}

// groupProfiles aggregates per-node profiles into one entry per device class.
func groupProfiles(profiles []Profile) []GPUGroup {
	byProduct := map[string]*aggregate{}
	order := make([]string, 0, len(profiles))
	for _, profile := range profiles {
		product := strings.TrimSpace(profile.Model)
		if product == "" {
			// Honest rather than invented: nothing described this device, and the control
			// plane refuses to place against a class it cannot identify.
			product = "unknown"
		}
		if _, present := byProduct[product]; !present {
			// Starts trusting and is demoted by any node that cannot be described, rather
			// than starting empty and being promoted by the first that can.
			byProduct[product] = &aggregate{memoryKnown: true, capabilityKnown: true}
			order = append(order, product)
		}
		byProduct[product].add(profile)
	}

	groups := make([]GPUGroup, 0, len(order))
	for _, product := range order {
		entry := byProduct[product]
		group := GPUGroup{Product: product, Count: entry.count}
		if entry.memoryKnown {
			group.MemoryBytes = int64(entry.memoryMiB) * mibToBytes
		}
		if entry.capabilityKnown && entry.capability.Known() {
			group.ComputeCapability = entry.capability.String()
		}
		groups = append(groups, group)
	}
	// Sorted so an unchanged cluster produces an identical report and the control plane
	// does not see a capability change on every heartbeat.
	sort.Slice(groups, func(i, j int) bool { return groups[i].Product < groups[j].Product })
	return groups
}
