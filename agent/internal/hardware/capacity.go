package hardware

import (
	"context"
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

// DeploymentIDLabel names which deployment a model-host pod belongs to.
//
// Claims are reported per deployment rather than as one stamp-wide number because the
// control plane compares what it has committed against what is running, and that comparison
// is only meaningful per deployment: on a stamp where one deployment's pods lead its
// placement rows and another's rows lead its pods, a single total looks identical to a stamp
// where neither diverges, and the two errors cancel into an overcommitment.
const DeploymentIDLabel = "fabric.khushwant.dev/deployment-id"

// MaxReportedClaims bounds the per-deployment list sent to the control plane, because the
// capability report is a bounded document by design. Hardware measurement itself retains all
// claims so the agent can put every live assignment ahead of terminating or out-of-band pods
// before applying this cap. It matches the control plane's supported placements-per-stamp
// limit and the schema's maxItems.
const MaxReportedClaims = 512

// DeploymentClaim is the devices one deployment's model hosts are holding.
type DeploymentClaim struct {
	DeploymentID string
	GPUs         int
}

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
	// MaxGPUsPerNode bounds what a single replica can ever ask for. A stamp-wide total
	// cannot answer whether one pod wanting four GPUs can be scheduled at all.
	MaxGPUsPerNode int
	// MaxFreeGPUsPerNode bounds what a single replica can be scheduled for *now*. A stamp
	// with two half-used two-device nodes has two devices free and no node that can take a
	// two-device pod, and the allocatable bound above cannot tell the difference.
	MaxFreeGPUsPerNode int
	// AvailableGPUSlots is indexed by GPUs per replica minus one. Entry k-1 is how many
	// k-device replicas fit into the currently free devices without splitting one replica
	// across nodes. The largest free node plus a stamp-wide total is still insufficient for
	// several replicas: free nodes [3,1] have four devices and can host only one 2-GPU pod.
	AvailableGPUSlots []int
	// RequestedGPUs is what scheduled pods have already claimed, all workloads included.
	RequestedGPUs int
	// FabricRequestedGPUs is the subset claimed by model hosts this platform created.
	FabricRequestedGPUs int
	// FabricClaims breaks that subset down by deployment, largest first, so the control
	// plane can compare each deployment's running pods against what it committed for that
	// deployment rather than against a stamp-wide total.
	FabricClaims []DeploymentClaim
	// Measured reports that the nodes were read. False means nothing here is trustworthy
	// and the caller should keep whatever it was configured with. It is always true when
	// Measure returned no error.
	Measured bool
	// PodsMeasured reports that pod claims were read. False leaves RequestedGPUs at zero,
	// which is indistinguishable from an idle cluster, so it is reported to the control
	// plane rather than only logged here.
	PodsMeasured bool
	// PodClaimsError explains a missing claim measurement, for the caller to log. Held as a
	// string rather than an error because Measure's error return is reserved for a failure
	// that makes the whole result unusable.
	PodClaimsError string
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

// MeasurePodGPURequests sums the devices scheduled pods have claimed on the given nodes,
// returning the total and the subset belonging to Fabric's own model hosts.
//
// Restricted to “onNodes“ because the nodes were themselves selected: a stamp that narrows
// its GPU node selector offers only those nodes' devices, and counting claims from the rest
// of the cluster would subtract GPUs from a total that never included them. On a shared
// cluster that arithmetic goes negative and the stamp refuses everything forever, with a
// reason pointing at capacity rather than at configuration.
//
// Only scheduled pods count. A pod with no node has not taken a device from anything, and
// counting it would make a stamp look full because somebody left an unschedulable pod
// behind. The cost is that a foreign pod about to be scheduled is invisible for a moment.
//
// Terminal pods are excluded server-side, so a node's history does not accumulate against
// its capacity.
func MeasurePodGPURequests(
	ctx context.Context, client Getter, onNodes map[string]bool,
) (PodClaims, error) {
	path := "/api/v1/pods?fieldSelector=" +
		"status.phase%21%3DSucceeded,status.phase%21%3DFailed"
	var pods podList
	if err := client.Get(ctx, path, &pods); err != nil {
		return PodClaims{}, fmt.Errorf("list gpu pods: %w", err)
	}

	claims := PodClaims{ByDeployment: map[string]int{}, ByNode: map[string]int{}}
	for _, item := range pods.Items {
		if item.Spec.NodeName == "" || !onNodes[item.Spec.NodeName] {
			continue
		}
		count := item.gpus()
		if count == 0 {
			continue
		}
		claims.Total += count
		// Per node as well as per deployment, because a replica is scheduled onto one node:
		// two devices free across two nodes cannot host a pod that needs two.
		claims.ByNode[item.Spec.NodeName] += count
		if item.Metadata.Labels["app.kubernetes.io/managed-by"] != FabricManagedByLabel {
			continue
		}
		claims.Fabric += count
		if deployment := item.Metadata.Labels[DeploymentIDLabel]; deployment != "" {
			claims.ByDeployment[deployment] += count
		}
		// A Fabric pod with no deployment label still counts in Fabric's total, so it is
		// charged as untracked rather than disappearing.
	}
	return claims, nil
}

// PodClaims is what scheduled pods have taken, and who took it.
type PodClaims struct {
	Total        int
	Fabric       int
	ByDeployment map[string]int
	ByNode       map[string]int
}

// largest returns the per-deployment claims ordered by size, capped so the report stays
// bounded. Ordered by size then id, so the same cluster produces the same document.
func (c PodClaims) largest(limit int) []DeploymentClaim {
	claims := make([]DeploymentClaim, 0, len(c.ByDeployment))
	for deployment, gpus := range c.ByDeployment {
		claims = append(claims, DeploymentClaim{DeploymentID: deployment, GPUs: gpus})
	}
	sort.Slice(claims, func(i, j int) bool {
		if claims[i].GPUs != claims[j].GPUs {
			return claims[i].GPUs > claims[j].GPUs
		}
		return claims[i].DeploymentID < claims[j].DeploymentID
	})
	if limit > 0 && len(claims) > limit {
		claims = claims[:limit]
	}
	return claims
}

// Measure reads what this cluster can offer.
//
// A non-nil error means nothing usable was obtained, so the ordinary Go shape — return on
// error, trust the value otherwise — is correct here. It is the node read that decides
// that: without it there is no hardware, no device class and no node set to scope claims to,
// and the caller should keep the capacity it was configured with. A missing permission
// should degrade to the declared number, not to a stamp that appears to have no hardware.
//
// A pod-read failure is not that: the hardware is known and only the claims are missing. It
// is recorded in PodsMeasured and PodClaimsError and travels to the control plane, because
// zero claimed GPUs is indistinguishable from an idle cluster and the difference decides
// placements.
func Measure(ctx context.Context, client Getter, selector map[string]string) (Capacity, error) {
	profiles, err := ProfileNodes(ctx, client, selector)
	if err != nil {
		return Capacity{}, err
	}

	capacity := Capacity{Measured: true, Profiles: profiles, GPUs: groupProfiles(profiles)}
	onNodes := make(map[string]bool, len(profiles))
	for _, profile := range profiles {
		capacity.AllocatableGPUs += profile.Count
		if profile.Count > capacity.MaxGPUsPerNode {
			capacity.MaxGPUsPerNode = profile.Count
		}
		onNodes[profile.Node] = true
	}

	claims, podErr := MeasurePodGPURequests(ctx, client, onNodes)
	if podErr != nil {
		// Without claims there is no way to know how much of any node is free, so the
		// schedulable width falls back to what the nodes advertise. It is the same bound
		// the report carried before claims were measured at all.
		capacity.MaxFreeGPUsPerNode = capacity.MaxGPUsPerNode
		capacity.PodClaimsError = podErr.Error()
		return capacity, nil
	}
	capacity.PodsMeasured = true
	capacity.RequestedGPUs = claims.Total
	capacity.FabricRequestedGPUs = claims.Fabric
	// Keep all measured claims in-process. The agent knows which deployments are live and
	// prioritizes them before applying the wire bound; truncating here would let a lingering
	// terminating or out-of-band pod permanently evict a supported live commitment.
	capacity.FabricClaims = claims.largest(0)
	capacity.AvailableGPUSlots = make([]int, 8) // ResourceSpec.gpu_count is bounded at eight.
	for _, profile := range profiles {
		free := max(0, profile.Count-claims.ByNode[profile.Node])
		if free > capacity.MaxFreeGPUsPerNode {
			capacity.MaxFreeGPUsPerNode = free
		}
		for width := 1; width <= len(capacity.AvailableGPUSlots); width++ {
			capacity.AvailableGPUSlots[width-1] += free / width
		}
	}
	return capacity, nil
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
