"""Environment capture and runner configuration."""

from __future__ import annotations

from harness import environment
from harness.artifact import TARGETS
from harness.runner import CORRECTNESS_SEEDS, build_config, parse_args


def test_capture_reports_versions_and_repository_state() -> None:
    captured = environment.capture()

    assert set(captured) == {
        "python_version",
        "platform",
        "torch_version",
        "torch_cuda_version",
        "triton_version",
        "cuda_available",
        "gpu",
        "driver_version",
        "git",
    }
    assert captured["python_version"].startswith("3.")
    assert set(captured["gpu"]) == {"name", "compute_capability", "memory_bytes"}
    # Runs from this repository must be attributable to a commit.
    assert captured["git"]["commit"]
    assert isinstance(captured["git"]["dirty"], bool)


def test_capture_never_raises_without_a_gpu(monkeypatch) -> None:
    monkeypatch.setattr(environment, "_gpu_state", lambda: {
        "torch_version": None,
        "torch_cuda_version": None,
        "triton_version": None,
        "cuda_available": False,
        "name": None,
        "compute_capability": None,
        "memory_bytes": None,
    })
    captured = environment.capture()
    assert captured["cuda_available"] is False
    assert captured["gpu"]["name"] is None


def test_missing_tool_returns_none() -> None:
    assert environment._command_output(["fabric-tool-that-does-not-exist"]) is None


def test_config_records_every_input_needed_to_reproduce() -> None:
    args = parse_args(["--target", "rtx4070-dev", "--dtype", "bfloat16", "--batches", "1", "4"])
    config = build_config(args)

    assert config["dtype"] == "bfloat16"
    assert config["batches"] == [1, 4]
    assert config["correctness_seeds"] == list(CORRECTNESS_SEEDS)
    assert config["state_dtype"] == "float32"
    assert config["shapes_are_synthetic"] is True
    assert config["benchmark_warmup"] and config["benchmark_reps"]
    # Drift and sweep inputs must be reproducible too.
    assert config["drift_steps"] == 1024
    assert config["drift_batch"] == 1
    assert config["sweep_block_v"] == [8, 16, 32]


def test_drift_and_sweep_can_be_disabled() -> None:
    args = parse_args(
        ["--target", "rtx4070-dev", "--drift-steps", "0", "--sweep-block-v"]
    )
    config = build_config(args)
    assert config["drift_steps"] == 0
    assert config["sweep_block_v"] == []


def test_every_roadmap_target_is_selectable() -> None:
    assert set(TARGETS) == {"rtx4070-dev", "a10-research", "t4-production"}
    # Only the release-gate target may back a production claim.
    assert [name for name, target in TARGETS.items() if target.citable_as_production] == [
        "t4-production"
    ]
