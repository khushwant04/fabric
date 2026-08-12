"""Occupancy arithmetic, byte accounting, and bandwidth utilization.

All pure computation, so it runs without a GPU. The counter-based metrics are
asserted to stay absent, because recording a plausible-looking number for
something that was never measured would be worse than recording nothing.
"""

from __future__ import annotations

from harness import profiling
from harness.compile_probe import parse_args as parse_probe_args

# RTX 4070 Laptop (SM89) limits, as reported by torch.
LIMITS = {
    "multiprocessors": 36,
    "max_threads_per_sm": 1536,
    "registers_per_sm": 65536,
    "shared_memory_per_sm": 102400,
    "warp_size": 32,
}


def test_occupancy_matches_hand_computation() -> None:
    """block_v=32 compiles to 40 registers, 1024 B shared, 8 warps."""
    result = profiling.theoretical_occupancy(
        registers=40, shared_memory_bytes=1024, num_warps=8, limits=LIMITS
    )
    assert result is not None
    assert result["threads_per_block"] == 256
    # registers: 65536 // (40*256) = 6; threads: 1536 // 256 = 6; shared: 102400 // 1024 = 100
    assert result["blocks_per_sm"] == 6
    assert result["active_warps_per_sm"] == 48
    assert result["max_warps_per_sm"] == 48
    assert result["occupancy"] == 1.0
    # The result is an upper bound, not a measurement.
    assert result["is_upper_bound"] is True


def test_occupancy_reports_the_binding_limit() -> None:
    register_bound = profiling.theoretical_occupancy(
        registers=200, shared_memory_bytes=1024, num_warps=8, limits=LIMITS
    )
    assert register_bound["limited_by"] == "registers"
    assert register_bound["occupancy"] < 1.0

    shared_bound = profiling.theoretical_occupancy(
        registers=32, shared_memory_bytes=51200, num_warps=4, limits=LIMITS
    )
    assert shared_bound["limited_by"] == "shared_memory"


def test_occupancy_is_none_when_metadata_is_missing() -> None:
    """Absent compiled metadata must not become a fabricated occupancy."""
    assert (
        profiling.theoretical_occupancy(
            registers=None, shared_memory_bytes=1024, num_warps=8, limits=LIMITS
        )
        is None
    )
    assert (
        profiling.theoretical_occupancy(
            registers=40, shared_memory_bytes=None, num_warps=8, limits=LIMITS
        )
        is None
    )
    assert (
        profiling.theoretical_occupancy(
            registers=40, shared_memory_bytes=1024, num_warps=8, limits={}
        )
        is None
    )


def test_bytes_moved_counts_the_state_twice() -> None:
    """The FP32 state is read and written once, and dominates the transfer."""
    moved = profiling.bytes_moved(
        batch=1, heads=8, key_dim=64, value_dim=64, element_size=2
    )
    state = 2 * 8 * 64 * 64 * 4
    qk = 2 * 8 * 64 * 2
    v_and_out = 2 * 8 * 64 * 2
    scalars = 2 * 8 * 4
    assert moved == state + qk + v_and_out + scalars
    assert state / moved > 0.9


def test_bytes_moved_scales_with_batch() -> None:
    single = profiling.bytes_moved(
        batch=1, heads=8, key_dim=64, value_dim=64, element_size=2
    )
    eight = profiling.bytes_moved(
        batch=8, heads=8, key_dim=64, value_dim=64, element_size=2
    )
    assert eight == 8 * single


def test_utilization_is_relative_to_measured_copy_bandwidth() -> None:
    result = profiling.utilization(
        bytes_per_launch=1_000_000, launch_ms=1.0, copy_bandwidth_gbps=10.0
    )
    assert result["achieved_gbps"] == 1.0
    assert result["copy_bandwidth_gbps"] == 10.0
    assert result["fraction_of_copy_bandwidth"] == 0.1


def test_zero_copy_bandwidth_does_not_divide_by_zero() -> None:
    result = profiling.utilization(
        bytes_per_launch=1000, launch_ms=1.0, copy_bandwidth_gbps=0.0
    )
    assert result["fraction_of_copy_bandwidth"] == 0.0


def test_counter_only_metrics_are_named_with_a_reason() -> None:
    assert "achieved_occupancy" in profiling.COUNTER_ONLY_METRICS
    assert "warp_execution_efficiency" in profiling.COUNTER_ONLY_METRICS
    assert "ERR_NVGPUCTRPERM" in profiling.COUNTER_METRICS_UNAVAILABLE


def test_compile_probe_defaults_are_reproducible() -> None:
    args = parse_probe_args([])
    assert (args.dtype, args.block_v, args.heads, args.key_dim, args.value_dim) == (
        "float16",
        32,
        8,
        64,
        64,
    )
