package operator

import (
	"context"
	"fmt"
	"math"
	"strconv"
	"strings"

	"github.com/khushwant04/fabric/agent/internal/hardware"
)

// Hardware profiling exists because serving settings are not preferences. Several of them
// are properties of the GPU, and getting one wrong does not degrade serving, it prevents
// it: a host asked for bfloat16 on a T4 exits before it listens, because compute
// capability 7.5 has no bfloat16 at all. That failure happened on this platform, from a
// chart default that was correct on the GPU it was written for.
//
// So the operator reads what the cluster reports about its GPU nodes and derives the
// settings that depend on hardware, rather than trusting a value a human typed once.
// Where a value is safe to leave alone it is left alone; where it cannot work it is
// overridden and the reason is recorded, because silently changing what an operator asked
// for is its own kind of failure.
//
// Reading the cluster now lives in internal/hardware, because the agent needs the same
// answer to report this stamp's capacity to the control plane (ADR 0013) and two tables of
// GPU facts would eventually disagree. What stays here is the part that is about the model
// host: turning a profile into settings.

// GPUProfile is what the platform knows about the GPUs on one node.
type GPUProfile = hardware.Profile

// ComputeCapability is a GPU's CUDA compute capability.
type ComputeCapability = hardware.ComputeCapability

// kubeGetter is the read this needs, kept narrow so it can be substituted in a test.
type kubeGetter = hardware.Getter

// ProfileGPUNodes returns a profile for every node advertising a GPU.
func ProfileGPUNodes(
	ctx context.Context, client kubeGetter, selector map[string]string,
) ([]GPUProfile, error) {
	return hardware.ProfileNodes(ctx, client, selector)
}

// Adjustment is one setting the profile changed, and why.
type Adjustment struct {
	Setting string
	From    string
	To      string
	Reason  string
}

// deviceHeadroomMiB is memory on the device that the model server's own pool must not
// claim.
//
// gpu-memory-utilization is a fraction of *total* device memory, and the server sizes its
// KV cache from it, but the CUDA context, the driver's own allocations, NCCL buffers and
// fragmentation are outside that accounting. On a large card a conventional 0.85 leaves
// gigabytes spare and the omission never shows. On a small one the same fraction can leave
// less than the runtime needs, and the server dies allocating rather than starting.
//
// One gibibyte is the floor observed to be sufficient for a single-device host. It is a
// floor on absolute memory rather than on the fraction, because the fraction is exactly
// what stops meaning anything as the device gets smaller.
const deviceHeadroomMiB = 1024

// applyProfile returns the host settings adjusted for the hardware, and what it changed.
//
// The only settings changed are those that cannot work as asked. A slower choice is left
// alone: overriding a value merely because it looks suboptimal would make the platform
// unpredictable, and the operator asking for it may know something this does not.
func applyProfile(host ModelHost, profiles []GPUProfile) (ModelHost, []Adjustment) {
	if len(profiles) == 0 {
		return host, nil
	}

	// The weakest capability and the smallest frame buffer present, because a host may be
	// scheduled onto any of them and a setting that only works on the best node is a
	// setting that fails intermittently.
	weakest := profiles[0]
	smallestMemory := profiles[0].MemoryMiB
	for _, profile := range profiles[1:] {
		if weakest.Capability.AtLeast(profile.Capability) {
			weakest = profile
		}
		if profile.MemoryMiB > 0 && (smallestMemory == 0 || profile.MemoryMiB < smallestMemory) {
			smallestMemory = profile.MemoryMiB
		}
	}

	var adjustments []Adjustment

	if strings.EqualFold(host.DType, "bfloat16") && weakest.Capability.Major > 0 && !weakest.SupportsBFloat16() {
		adjustments = append(adjustments, Adjustment{
			Setting: "dtype",
			From:    host.DType,
			To:      "float16",
			Reason: fmt.Sprintf(
				"%s is compute capability %s, which has no bfloat16; a host asked for it exits before serving",
				weakest.Model, weakest.Capability,
			),
		})
		host.DType = "float16"
	}

	if adjusted, change := clampMemoryUtilization(host.GPUMemoryUtilization, smallestMemory); change != nil {
		adjustments = append(adjustments, *change)
		host.GPUMemoryUtilization = adjusted
	}

	return host, adjustments
}

// clampMemoryUtilization lowers a memory fraction that would leave the device with less
// absolute headroom than the runtime needs.
//
// It only ever lowers. A fraction that already leaves enough is returned untouched, and a
// device too small to leave the headroom at any fraction is left alone as well: there is no
// value that makes such a host work, and inventing one would replace a clear allocation
// failure with a confusing one.
func clampMemoryUtilization(configured string, smallestMemoryMiB int) (string, *Adjustment) {
	if configured == "" || smallestMemoryMiB <= 0 {
		return configured, nil
	}
	fraction, err := strconv.ParseFloat(configured, 64)
	if err != nil || fraction <= 0 || fraction > 1 {
		// Not this function's error to report: the value goes to the server unchanged and
		// the server rejects it, which names the real problem.
		return configured, nil
	}
	// Below twice the headroom there is no fraction worth serving from, so the setting is
	// not the thing that is wrong.
	if smallestMemoryMiB <= 2*deviceHeadroomMiB {
		return configured, nil
	}

	ceiling := float64(smallestMemoryMiB-deviceHeadroomMiB) / float64(smallestMemoryMiB)
	if fraction <= ceiling {
		return configured, nil
	}
	// Floored to two decimals rather than rounded, so the result is never nudged back above
	// the ceiling it was computed from.
	clamped := math.Floor(ceiling*100) / 100
	formatted := strconv.FormatFloat(clamped, 'f', 2, 64)
	return formatted, &Adjustment{
		Setting: "gpu-memory-utilization",
		From:    configured,
		To:      formatted,
		Reason: fmt.Sprintf(
			"the smallest GPU has %d MiB, and %s of it leaves under %d MiB for the CUDA "+
				"context and driver allocations the fraction does not account for; a host "+
				"asked for it dies allocating instead of starting",
			smallestMemoryMiB, configured, deviceHeadroomMiB,
		),
	}
}
