"""Kernel profiling that does not require GPU performance counters.

The benchmark plan asks for occupancy, DRAM utilization, warp efficiency, and
compile/first-use latency. Counter-based metrics need Nsight Compute with
admin-enabled profiling; on a host without that permission Nsight reports
``ERR_NVGPUCTRPERM`` and collects nothing.

Rather than leave those metrics silently missing, this module computes what is
derivable without counters and records why the rest is absent:

* **Theoretical occupancy** from compiled register/shared-memory/warp counts and
  device limits. This is arithmetic, not a measurement.
* **DRAM utilization** as achieved bytes per second over a *measured* device copy
  bandwidth, so the denominator is reproducible on the same host rather than a
  vendor peak figure.
* **Warp efficiency** stays ``None``: it cannot be derived without counters.
"""

from __future__ import annotations

from typing import Any

import torch

#: Recorded when counter-based metrics are unavailable.
COUNTER_METRICS_UNAVAILABLE = (
    "Requires Nsight Compute with admin-enabled GPU performance counters "
    "(ERR_NVGPUCTRPERM when profiling is restricted to admin users)."
)

#: Metrics that are only obtainable from hardware counters.
COUNTER_ONLY_METRICS = ("achieved_occupancy", "warp_execution_efficiency", "dram_counters")


def device_limits(properties: Any) -> dict[str, int | None]:
    """Return the occupancy-relevant limits of a CUDA device."""
    return {
        "multiprocessors": getattr(properties, "multi_processor_count", None),
        "max_threads_per_sm": getattr(properties, "max_threads_per_multi_processor", None),
        "registers_per_sm": getattr(properties, "regs_per_multiprocessor", None),
        "shared_memory_per_sm": getattr(properties, "shared_memory_per_multiprocessor", None),
        "warp_size": getattr(properties, "warp_size", 32),
    }


def theoretical_occupancy(
    *,
    registers: int | None,
    shared_memory_bytes: int | None,
    num_warps: int | None,
    limits: dict[str, int | None],
) -> dict[str, Any] | None:
    """Compute static occupancy from compiled metadata and device limits.

    Ignores register allocation granularity and the hardware maximum blocks per
    SM, neither of which torch exposes, so the result is an upper bound. Returns
    ``None`` when any input is missing.
    """
    warp_size = limits.get("warp_size") or 32
    max_threads = limits.get("max_threads_per_sm")
    registers_per_sm = limits.get("registers_per_sm")
    shared_per_sm = limits.get("shared_memory_per_sm")
    if not all(
        isinstance(value, int) and value > 0
        for value in (registers, num_warps, max_threads, registers_per_sm, shared_per_sm)
    ):
        return None
    if shared_memory_bytes is None:
        return None

    threads_per_block = num_warps * warp_size
    blocks_by_registers = registers_per_sm // max(registers * threads_per_block, 1)
    blocks_by_shared = (
        shared_per_sm // shared_memory_bytes if shared_memory_bytes > 0 else max_threads
    )
    blocks_by_threads = max_threads // threads_per_block
    blocks_per_sm = min(blocks_by_registers, blocks_by_shared, blocks_by_threads)

    max_warps_per_sm = max_threads // warp_size
    active_warps = blocks_per_sm * num_warps
    return {
        "threads_per_block": threads_per_block,
        "blocks_per_sm": blocks_per_sm,
        "limited_by": min(
            (
                (blocks_by_registers, "registers"),
                (blocks_by_shared, "shared_memory"),
                (blocks_by_threads, "threads"),
            )
        )[1],
        "active_warps_per_sm": active_warps,
        "max_warps_per_sm": max_warps_per_sm,
        "occupancy": active_warps / max_warps_per_sm,
        "is_upper_bound": True,
    }


def bytes_moved(
    *,
    batch: int,
    heads: int,
    key_dim: int,
    value_dim: int,
    element_size: int,
    state_element_size: int = 4,
) -> int:
    """Bytes one decode step must move, counting the state read and write.

    Q, K, V, and the output are model dtype; g, beta, and the recurrent state are
    FP32. The state dominates, which is why it is read and written once each.
    """
    pairs = batch * heads
    qk = 2 * pairs * key_dim * element_size
    v_and_out = 2 * pairs * value_dim * element_size
    scalars = 2 * pairs * 4
    state = 2 * pairs * key_dim * value_dim * state_element_size
    return qk + v_and_out + scalars + state


def measure_copy_bandwidth(size_bytes: int = 256 * 1024 * 1024, reps: int = 20) -> float:
    """Measure achievable device copy bandwidth in GB/s.

    Used as the DRAM-utilization denominator so the figure is grounded in a
    number reproducible on this host instead of a vendor peak.
    """
    elements = size_bytes // 4
    source = torch.empty(elements, dtype=torch.float32, device="cuda")
    destination = torch.empty_like(source)

    destination.copy_(source)
    torch.cuda.synchronize()
    start = torch.cuda.Event(enable_timing=True)
    end = torch.cuda.Event(enable_timing=True)
    start.record()
    for _ in range(reps):
        destination.copy_(source)
    end.record()
    torch.cuda.synchronize()

    elapsed_s = start.elapsed_time(end) / 1e3
    moved = 2 * source.numel() * source.element_size() * reps
    return moved / elapsed_s / 1e9


def utilization(
    *, bytes_per_launch: int, launch_ms: float, copy_bandwidth_gbps: float
) -> dict[str, float]:
    """Express achieved bandwidth as a fraction of measured copy bandwidth."""
    achieved = bytes_per_launch / (launch_ms * 1e-3) / 1e9
    return {
        "achieved_gbps": achieved,
        "copy_bandwidth_gbps": copy_bandwidth_gbps,
        "fraction_of_copy_bandwidth": achieved / copy_bandwidth_gbps
        if copy_bandwidth_gbps
        else 0.0,
    }
