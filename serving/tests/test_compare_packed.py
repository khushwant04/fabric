"""CPU checks for shape attribution and fail-closed stock-op benchmark gates."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
import torch
from harness.artifact import read_artifact, resolve_target

from fabric_serving import compare_packed as benchmark

SHAPES = {"H": 1, "HV": 2, "K": 2, "V": 2}


def _config(**overrides):
    return {
        "model_type": "qwen3_5",
        "text_config": {
            "model_type": "qwen3_5_text",
            "linear_num_key_heads": 16,
            "linear_num_value_heads": 32,
            "linear_key_head_dim": 128,
            "linear_value_head_dim": 128,
            **overrides,
        },
    }


def _stock(qkv, _a, _b, _A_log, _bias, _scale, state, out, indices, _normalize, **kwargs):
    for position, slot in enumerate(indices.tolist()):
        state[slot].add_(0.125)
        out[position].fill_(state[slot].mean().item() + qkv[position, 0].item())
    return out, state


def _check(candidate, **overrides):
    values = {"kwargs": {}, "drift_steps": 16, "atol": 0.0, "rtol": 0.0, "device": "cpu"}
    return benchmark.check_candidate(
        _stock, candidate, 2, SHAPES, torch.float32, **{**values, **overrides}
    )


def test_config_uses_real_grouped_value_heads_and_records_identity(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(_config()))
    shapes, metadata = benchmark.load_model_config(path)
    assert shapes == {"H": 16, "HV": 32, "K": 128, "V": 128}
    assert metadata["shape_source"] == "model_config"
    assert len(metadata["model_config_sha256"]) == 64
    assert metadata["model_type"] == "qwen3_5_text"


def test_launch_shape_is_labeled_and_not_inferred_from_model_name():
    shapes, metadata = benchmark.load_model_config(None)
    assert shapes == benchmark.LAUNCH_MODEL
    assert metadata["shape_source"] == "legacy_launch_shape"


@pytest.mark.parametrize(
    "field,value",
    [
        ("linear_num_key_heads", 0),
        ("linear_num_value_heads", 31),
        ("linear_key_head_dim", True),
        ("linear_value_head_dim", 128.0),
    ],
)
def test_invalid_config_dimensions_fail_closed(tmp_path, field, value):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(_config(**{field: value})))
    with pytest.raises(ValueError):
        benchmark.load_model_config(path)


def test_non_gated_delta_or_missing_explicit_dimensions_are_rejected(tmp_path):
    path = tmp_path / "config.json"
    for config in (
        {"model_type": "llama"},
        {"model_type": "qwen3_5_text"},
        {"model_type": []},
    ):
        path.write_text(json.dumps(config))
        with pytest.raises(ValueError):
            benchmark.load_model_config(path)


def test_cpu_inputs_have_grouped_shapes_extra_slots_and_local_generator():
    torch.manual_seed(123)
    before = torch.random.get_rng_state().clone()
    inputs = benchmark.make_inputs(2, SHAPES, torch.float32, device="cpu")
    assert inputs["mixed_qkv"].shape == (2, 8)
    assert inputs["state"].shape == (5, 2, 2, 2)
    assert inputs["indices"].unique().numel() == 2
    assert inputs["indices"].min() > 0
    assert torch.equal(torch.random.get_rng_state(), before)


@pytest.mark.parametrize("dtype", [torch.float16, torch.bfloat16, torch.float32])
def test_gate_projection_inputs_use_model_dtype_for_stock_sigmoid_rounding(dtype):
    inputs = benchmark.make_inputs(2, SHAPES, dtype, device="cpu")
    assert inputs["mixed_qkv"].dtype == inputs["a"].dtype == inputs["b"].dtype == dtype
    assert inputs["state"].dtype == torch.float32


def test_correct_candidate_passes_cold_and_independent_drift_gate():
    check = _check(_stock)
    assert check["status"] == "pass"
    assert check["cold_seeds"] == [0, 1, 17]
    assert check["drift_seed"] not in check["cold_seeds"]
    assert check["drift_steps"] == 16
    assert check["drift_max_state_difference"] == 0
    assert check["full_state_checked"]


def test_correct_output_with_inactive_slot_corruption_fails():
    def corrupt(*args, **kwargs):
        result = _stock(*args, **kwargs)
        args[6][0].add_(1)
        return result

    check = _check(corrupt)
    assert check["status"] == "fail"
    assert check["failure_phase"] == "cold"
    assert check["max_state_difference"] == 1


def test_nonfinite_outputs_never_pass_even_with_tolerance():
    def corrupt(*args, **kwargs):
        result = _stock(*args, **kwargs)
        args[7].fill_(float("nan"))
        return result

    check = _check(corrupt, atol=1.0, rtol=1.0)
    assert check["status"] == "fail"
    assert "non-finite" in check["error"]
    json.dumps(check, allow_nan=False)


def test_tolerance_cannot_hide_recurrent_accumulation():
    def accumulates(*args, **kwargs):
        result = _stock(*args, **kwargs)
        args[6][0].add_(0.001)
        return result

    check = _check(accumulates, atol=0.005)
    assert check["status"] == "fail"
    assert check["failure_phase"] == "drift"
    assert check["failure_step"] > 1


def test_each_swept_tile_is_gated_before_timing_and_bad_tiles_are_retained(monkeypatch):
    events = []

    def gate(stock, candidate, batch, shapes, dtype, *, kwargs, **options):
        label = f"{kwargs['block_v']}/{kwargs['num_warps']}" if kwargs else "fabric"
        events.append(("check", label))
        return {
            "status": "fail" if label == "16/1" else "pass",
            "max_output_difference": 0.0,
            "max_state_difference": 0.0,
            "error": "bad tile" if label == "16/1" else None,
        }

    def time(candidates, inputs, dtype, rounds, **options):
        assert "16/1" not in candidates
        assert len(events) == 14
        events.append(("timing", sorted(candidates)))
        return {label: 10.0 if label == "vllm" else 5.0 for label in candidates}

    monkeypatch.setattr(benchmark, "check_candidate", gate)
    monkeypatch.setattr(benchmark, "compare_interleaved", time)
    monkeypatch.setattr(benchmark, "make_inputs", lambda *a, **k: {})
    args = SimpleNamespace(sweep=True, drift_steps=16, atol=0.0, rtol=0.0, reps=60)
    result = benchmark.compare_batch(_stock, 1, benchmark.LAUNCH_MODEL, torch.float32, args)
    assert result["status"] == "fail"
    assert result["candidates"]["16/1"]["error"] == "bad tile"
    assert (result["best_block_v"], result["best_num_warps"]) != (16, 1)


def test_failed_default_is_never_timed(monkeypatch):
    monkeypatch.setattr(
        benchmark,
        "check_candidate",
        lambda *a, **k: {
            "status": "fail",
            "max_output_difference": 1.0,
            "max_state_difference": 1.0,
        },
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("failed candidate was timed")

    monkeypatch.setattr(benchmark, "compare_interleaved", forbidden)
    args = SimpleNamespace(sweep=False, drift_steps=16, atol=0.0, rtol=0.0, reps=60)
    result = benchmark.compare_batch(_stock, 1, SHAPES, torch.float32, args)
    assert result["status"] == "fail"
    assert result["fabric_us"] is None and "best_us" not in result


def test_timing_or_capture_failure_is_retained_and_not_recommended(monkeypatch):
    monkeypatch.setattr(
        benchmark,
        "check_candidate",
        lambda *a, **k: {
            "status": "pass",
            "max_output_difference": 0.0,
            "max_state_difference": 0.0,
        },
    )
    monkeypatch.setattr(benchmark, "make_inputs", lambda *a, **k: {})

    def time(candidates, inputs, dtype, rounds, *, failures):
        failures["16/1"] = "graph capture failed"
        return {label: 10.0 for label in candidates if label != "16/1"}

    monkeypatch.setattr(benchmark, "compare_interleaved", time)
    args = SimpleNamespace(sweep=True, drift_steps=16, atol=0.0, rtol=0.0, reps=60)
    result = benchmark.compare_batch(_stock, 1, benchmark.LAUNCH_MODEL, torch.float32, args)
    assert result["status"] == "fail"
    assert result["candidates"]["16/1"]["failure_phase"] == "timing"
    assert (result["best_block_v"], result["best_num_warps"]) != (16, 1)


def test_interleaved_sampler_contains_candidate_failures(monkeypatch):
    def build(fn, inputs, dtype, **kwargs):
        if fn == "bad":
            raise RuntimeError("capture failed")
        return lambda: 10.0

    monkeypatch.setattr(benchmark, "_sampler", build)
    monkeypatch.setattr(benchmark, "_USE_GRAPH", False)
    failures = {}
    timings = benchmark.compare_interleaved(
        {"stock": ("ok", {}), "bad-tile": ("bad", {})},
        {},
        torch.float32,
        3,
        failures=failures,
    )
    assert timings == {"stock": 10.0}
    assert "capture failed" in failures["bad-tile"]


def _fake_cuda(monkeypatch, name="Tesla T4"):
    monkeypatch.setattr(torch.cuda, "is_available", lambda: True)
    captured = {"gpu": {"name": name, "compute_capability": "7.5"}, "git": {"commit": "abc"}}
    monkeypatch.setattr(benchmark.environment, "capture", lambda: captured)
    return captured


def test_uninstalled_vllm_returns_actionable_error_instead_of_none_attribute_crash(
    monkeypatch, capsys
):
    _fake_cuda(monkeypatch)
    monkeypatch.setattr(benchmark, "resolve", lambda: None)
    assert benchmark.main(["--batch", "1"]) == 2
    assert "unavailable" in capsys.readouterr().err


def test_wrong_target_blocks_before_resolving_or_timing(monkeypatch, capsys):
    _fake_cuda(monkeypatch, name="RTX 4070 Laptop GPU")

    def forbidden():
        raise AssertionError("stock op resolved before target guard")

    monkeypatch.setattr(benchmark, "resolve", forbidden)
    assert benchmark.main(["--target", "t4-production"]) == 2
    assert "expects a GPU" in capsys.readouterr().err


@pytest.mark.parametrize(
    "flags",
    [
        ["--drift-steps", "0"],
        ["--batch", "0"],
        ["--atol", "nan"],
        ["--artifact-root", "/tmp/evidence"],
    ],
)
def test_invalid_cli_inputs_fail_before_cuda(flags):
    with pytest.raises(SystemExit) as exc:
        benchmark.main(flags)
    assert exc.value.code == 2


def test_json_keeps_real_config_metadata_and_failure_exit(monkeypatch, tmp_path, capsys):
    _fake_cuda(monkeypatch)
    monkeypatch.setattr(
        benchmark,
        "resolve",
        lambda: SimpleNamespace(
            packed=_stock,
            has_packed_decode=True,
            version="test",
            module="stock.test",
        ),
    )

    def compare(stock, batch, shapes, dtype, args):
        assert shapes["HV"] == 32
        return {
            "batch": batch,
            "status": "fail",
            "candidates": {
                "fabric": {"status": "fail", "error": "wrong state"},
            },
            "fabric_us": None,
        }

    monkeypatch.setattr(benchmark, "compare_batch", compare)
    config = tmp_path / "config.json"
    config.write_text(json.dumps(_config()))
    assert (
        benchmark.main(
            [
                "--batch",
                "1",
                "--model-config",
                str(config),
                "--model-ref",
                "Qwen/Qwen3.5-4B",
                "--model-revision",
                "revision-hash",
                "--json",
            ]
        )
        == 1
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["model_ref"] == "Qwen/Qwen3.5-4B"
    assert payload["model_revision"] == "revision-hash"
    assert payload["results"][0]["candidates"]["fabric"]["error"] == "wrong state"


def test_artifact_uses_existing_schema_and_never_overwrites(tmp_path, monkeypatch):
    captured = {"gpu": {"name": "Tesla T4"}, "git": {"commit": "abc"}}
    payload = {
        "status": "fail",
        "vllm_version": "test",
        "vllm_module": "stock.test",
        "sweep": True,
        "drift_steps": 16,
        "model_revision": "pinned-revision",
        "results": [
            {
                "batch": 1,
                "candidates": {
                    "16/1": {
                        "status": "fail",
                        "error": "wrong next state",
                    }
                },
            }
        ],
    }
    fixed = tmp_path / "t4-production" / "fixed.json"
    monkeypatch.setattr(benchmark, "artifact_path", lambda *args: fixed)
    target = resolve_target("t4-production")
    path = benchmark._export_artifact(tmp_path, target, captured, payload)
    artifact = read_artifact(path)
    assert artifact["status"] == "fail"
    assert artifact["config"]["model_revision"] == "pinned-revision"
    assert artifact["correctness"][0]["error"] == "wrong next state"
    assert "Not a full-model" in artifact["claim_scope"]
    with pytest.raises(FileExistsError):
        benchmark._export_artifact(tmp_path, target, captured, payload)
