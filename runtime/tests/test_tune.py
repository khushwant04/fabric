"""CPU checks for the launch-shape sweep: grid, gating, selection, output, target refusal."""

from __future__ import annotations

import json

import pytest
import torch

from harness import tune
from kernels import launch_config as lc
from kernels.gated_delta_packed_decode import torch_packed_decode_reference

SHAPES = {"H": 1, "HV": 2, "K": 8, "V": 8}


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for variable in (lc.TUNING_FILE_ENV, lc.ENV_BLOCK_V, lc.ENV_NUM_WARPS, lc.ENV_NUM_STAGES):
        monkeypatch.delenv(variable, raising=False)
    lc.reload()
    yield
    monkeypatch.delenv(lc.TUNING_FILE_ENV, raising=False)
    lc.reload()


def test_stages_swept_on_turing_are_limited():
    assert tune.default_stages((7, 5)) == (1, 2, 3)
    assert tune.default_stages((8, 6)) == (1, 2, 3, 4)


def test_candidate_grid_drops_collapsed_tiles_and_idle_warps():
    grid = tune.candidate_grid(128, (16, 32, 64, 128, 256), (1, 2, 4, 8), (1, 2))
    labels = {tune.label_of(config) for config in grid}
    assert "16/1/1" in labels and "16/2/2" in labels and "16/4/1" not in labels
    assert "128/8/2" in labels
    assert not any(config["block_v"] == 256 for config in grid)
    assert len(grid) == (2 + 3 + 4 + 4) * 2
    narrow = tune.candidate_grid(24, (16, 32, 64), (1,), (1,))
    assert {config["block_v"] for config in narrow} == {16, 32}


def _reference_kernel(*args, **_launch):
    return torch_packed_decode_reference(*args)


def test_correct_candidate_passes_and_is_bitwise_equal_to_default():
    check = tune.check_candidate(
        _reference_kernel,
        torch_packed_decode_reference,
        {"block_v": 16, "num_warps": 1, "num_stages": 1},
        {"block_v": 32, "num_warps": 1, "num_stages": 3},
        2,
        SHAPES,
        torch.float32,
        drift_steps=4,
        atol=0.0,
        rtol=0.0,
        device="cpu",
    )
    assert check["status"] == "pass", check
    assert check["bitwise_equal_to_default"] is True
    assert check["full_state_checked"]
    assert "failure_step" not in check


def test_drifting_candidate_fails_in_the_recurrent_phase():
    def drifts(*args, **launch):
        result = torch_packed_decode_reference(*args)
        if launch.get("block_v") == 64:
            args[6].add_(2e-7)  # below tolerance once, not after several recurrent steps
        return result

    check = tune.check_candidate(
        drifts,
        torch_packed_decode_reference,
        {"block_v": 64, "num_warps": 2, "num_stages": 1},
        {"block_v": 32, "num_warps": 1, "num_stages": 3},
        2,
        SHAPES,
        torch.float32,
        drift_steps=16,
        atol=1e-6,
        rtol=0.0,
        device="cpu",
    )
    assert check["status"] == "fail"
    assert check["failure_phase"] == "drift"
    assert check["failure_step"] > 1
    assert check["bitwise_equal_to_default"] is False


def test_selection_uses_relative_latency_across_the_bucket():
    configs = {
        "16/1/1": {"block_v": 16, "num_warps": 1, "num_stages": 1},
        "32/1/1": {"block_v": 32, "num_warps": 1, "num_stages": 1},
        "64/2/1": {"block_v": 64, "num_warps": 2, "num_stages": 1},
    }
    timings = {
        1: {"16/1/1": 10.0, "32/1/1": 15.0, "64/2/1": 30.0},
        # 64/2/1 wins outright at batch 4 but was not timed at batch 2: ineligible.
        2: {"16/1/1": 12.0, "32/1/1": 11.0},
        4: {"16/1/1": 30.0, "32/1/1": 20.0, "64/2/1": 18.0},
        32: {"16/1/1": 100.0, "32/1/1": 100.0},
    }
    chosen = tune.select_per_bucket(timings, configs)
    assert chosen["1"]["label"] == "16/1/1"
    assert chosen["2-4"]["label"] == "32/1/1"
    assert chosen["2-4"]["batches"] == [2, 4]
    # A tie goes to the smaller tile.
    assert chosen["17+"]["label"] == "16/1/1"
    assert "5-16" not in chosen


def test_tuning_document_round_trips_through_the_consumer(tmp_path, monkeypatch):
    selections = {
        "1": {"block_v": 16, "num_warps": 1, "num_stages": 1, "batches": [1], "us": {"1": 9.0}},
        "17+": {
            "block_v": 32,
            "num_warps": 1,
            "num_stages": 2,
            "batches": [32, 64],
            "us": {"32": 50.0, "64": 99.0},
        },
    }
    document = tune.build_tuning_document(
        selections=selections,
        gpu_name="Tesla T4",
        capability="7.5",
        target="t4-production",
        context={"dtype": "float16", "shapes": SHAPES, "cuda_graph": True},
        artifact={"path": "runtime/artifacts/t4-production/x.json", "content_hash": "0" * 64},
        created_at="2026-11-16T00:00:00Z",
    )
    path = tune.write_exclusive(tmp_path / "tuning" / "t4.tuning.json", document)
    with pytest.raises(FileExistsError):
        tune.write_exclusive(path, document)

    stored = json.loads(path.read_text())
    assert stored["gpu"] == {"name": "Tesla T4", "compute_capability": "7.5"}
    assert [entry["batch_bucket"] for entry in stored["entries"]] == ["1", "17+"]
    assert all(entry["measured"] for entry in stored["entries"])

    monkeypatch.setenv(lc.TUNING_FILE_ENV, str(path))
    lc.reload()
    first = lc.resolve(lc.KERNEL_PACKED, (7, 5), 1)
    assert (first.block_v, first.num_warps, first.num_stages) == (16, 1, 1)
    assert first.measured and first.source == f"tuning-file:{path}"
    wide = lc.resolve(lc.KERNEL_PACKED, (7, 5), 48)
    assert (wide.block_v, wide.num_warps, wide.num_stages) == (32, 1, 2)
    # Another GPU's capability is untouched by a T4 file.
    assert lc.resolve(lc.KERNEL_PACKED, (8, 6), 1).source == "table"


def test_tuning_path_sits_beside_the_artifact(tmp_path):
    artifact = tmp_path / "t4-production" / "20261116T000000Z-abc.json"
    assert tune.tuning_path_for(artifact) == (
        tmp_path / "t4-production" / "tuning" / "20261116T000000Z-abc.tuning.json"
    )


def test_target_mismatch_is_refused_before_anything_runs(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(tune.torch.cuda, "is_available", lambda: True)
    monkeypatch.setattr(
        tune.environment,
        "capture",
        lambda: {"gpu": {"name": "NVIDIA A10", "compute_capability": "8.6"}},
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("a candidate was checked for a mismatched target")

    monkeypatch.setattr(tune, "check_candidate", forbidden)
    code = tune.main(["--target", "t4-production", "--artifact-root", str(tmp_path)])
    assert code == 2
    assert "T4" in capsys.readouterr().err
    assert not any(tmp_path.iterdir())


def test_bf16_sweep_on_turing_is_refused(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(tune.torch.cuda, "is_available", lambda: True)
    monkeypatch.setattr(
        tune.environment,
        "capture",
        lambda: {"gpu": {"name": "Tesla T4", "compute_capability": "7.5"}},
    )
    code = tune.main(
        ["--target", "t4-production", "--dtype", "bfloat16", "--artifact-root", str(tmp_path)]
    )
    assert code == 2
    assert "--dtype half" in capsys.readouterr().err


def test_sweep_requires_a_gpu_and_a_known_target(tmp_path, monkeypatch):
    monkeypatch.setattr(tune.torch.cuda, "is_available", lambda: False)
    assert tune.main(["--target", "a10-research", "--artifact-root", str(tmp_path)]) == 2
    with pytest.raises(SystemExit):
        tune.main(["--target", "h100-somewhere"])
    with pytest.raises(SystemExit):
        tune.main([])
