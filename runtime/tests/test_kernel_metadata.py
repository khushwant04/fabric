"""Compiled-kernel metadata capture and tile-sweep analysis.

These exercise the guarded Triton-internal reads with fakes, so they run without
a GPU and still fail if the guards are removed.
"""

from __future__ import annotations

from typing import Any

from harness import kernel_metadata
from harness.runner import fastest_tile_per_batch


class FakeMetadata:
    def __init__(self, shared: int, num_warps: int) -> None:
        self.shared = shared
        self.num_warps = num_warps
        self.num_stages = 3
        self.name = "_gated_delta_decode_kernel"


class FakeCompiled:
    def __init__(self, regs: int, spills: int, shared: int, num_warps: int) -> None:
        self.n_regs = regs
        self.n_spills = spills
        self.metadata = FakeMetadata(shared, num_warps)


class FakeKernel:
    """Mimics Triton's ``device_caches`` layout: device -> (compiled, ...)."""

    def __init__(self) -> None:
        self.device_caches: dict[int, tuple[dict[str, Any], str]] = {0: ({}, "target")}

    def compile(self, key: str, compiled: FakeCompiled) -> None:
        self.device_caches[0][0][key] = compiled


def test_describe_maps_every_recorded_metric() -> None:
    described = kernel_metadata.describe(FakeCompiled(40, 0, 1024, 8))
    assert described == {
        "registers": 40,
        "spills": 0,
        "shared_memory_bytes": 1024,
        "num_warps": 8,
        "num_stages": 3,
        "name": "_gated_delta_decode_kernel",
    }


def test_capture_attributes_the_newly_compiled_kernel() -> None:
    kernel = FakeKernel()
    kernel.compile("existing", FakeCompiled(1, 1, 1, 1))

    captured = kernel_metadata.capture(
        kernel, lambda: kernel.compile("block_v=16", FakeCompiled(40, 0, 256, 4))
    )
    assert captured == {
        "registers": 40,
        "spills": 0,
        "shared_memory_bytes": 256,
        "num_warps": 4,
        "num_stages": 3,
        "name": "_gated_delta_decode_kernel",
    }


def test_capture_returns_none_when_nothing_new_compiled() -> None:
    """An already-compiled configuration cannot be attributed to this launch."""
    kernel = FakeKernel()
    kernel.compile("warm", FakeCompiled(40, 0, 256, 4))
    assert kernel_metadata.capture(kernel, lambda: None) is None


def test_capture_survives_an_unknown_cache_layout() -> None:
    class Alien:
        device_caches = {0: "not-a-tuple-of-dicts"}

    launched: list[bool] = []
    assert kernel_metadata.capture(Alien(), lambda: launched.append(True)) is None
    # The measurement still ran; only the metadata is absent.
    assert launched == [True]


def test_capture_handles_a_kernel_without_caches() -> None:
    class Bare:
        pass

    assert kernel_metadata.capture(Bare(), lambda: None) is None


def test_describe_reports_absent_fields_as_none() -> None:
    class Opaque:
        pass

    described = kernel_metadata.describe(Opaque())
    assert set(described) == {
        "registers",
        "spills",
        "shared_memory_bytes",
        "num_warps",
        "num_stages",
        "name",
    }
    assert all(value is None for value in described.values())


def test_fastest_tile_is_chosen_per_batch() -> None:
    sweep = [
        {
            "block_v": 8,
            "kernel": None,
            "timings": [
                {"batch": 1, "triton_ms": 0.013},
                {"batch": 8, "triton_ms": 0.020},
            ],
        },
        {
            "block_v": 16,
            "kernel": None,
            "timings": [
                {"batch": 1, "triton_ms": 0.012},
                {"batch": 8, "triton_ms": 0.019},
            ],
        },
        {
            "block_v": 32,
            "kernel": None,
            "timings": [
                {"batch": 1, "triton_ms": 0.017},
                {"batch": 8, "triton_ms": 0.015},
            ],
        },
    ]
    assert fastest_tile_per_batch(sweep) == {"1": 16, "8": 32}
