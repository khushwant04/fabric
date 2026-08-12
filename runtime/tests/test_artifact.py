"""Artifact schema, hashing, and target-hardware guard."""

from __future__ import annotations

import datetime as dt
import json
import pathlib

import pytest

from harness.artifact import (
    SCHEMA_VERSION,
    SUPPORTED_SCHEMA_VERSIONS,
    TargetMismatchError,
    UnsupportedArtifactError,
    build_artifact,
    content_hash,
    read_artifact,
    resolve_target,
    verify_artifact,
    verify_target,
    write_artifact,
)

ENVIRONMENT = {
    "python_version": "3.12.0",
    "platform": "Linux-test",
    "torch_version": "2.8.0+cu128",
    "torch_cuda_version": "12.8",
    "triton_version": "3.4.0",
    "cuda_available": True,
    "gpu": {
        "name": "NVIDIA GeForce RTX 4070 Laptop GPU",
        "compute_capability": "8.9",
        "memory_bytes": 8585216000,
    },
    "driver_version": "592.82",
    "git": {"commit": "a" * 40, "dirty": False},
}


def _artifact(**overrides):
    kwargs = {
        "target": resolve_target("rtx4070-dev"),
        "suite": "gated-delta-decode-microbench",
        "status": "pass",
        "environment": ENVIRONMENT,
        "config": {"dtype": "float16", "batches": [1, 2]},
        "correctness": [{"batch": 1, "output_max_abs": 1e-4}],
        "measurements": [{"batch": 1, "triton_ms": 0.05}],
        "created_at": dt.datetime(2026, 8, 12, 11, 30, tzinfo=dt.UTC),
        "run_id": "f" * 32,
    }
    kwargs.update(overrides)
    return build_artifact(**kwargs)


def test_artifact_records_target_role_and_claim_scope() -> None:
    artifact = _artifact()
    assert artifact["schema_version"] == SCHEMA_VERSION
    assert artifact["target"] == "rtx4070-dev"
    assert artifact["target_role"] == "development and regression"
    # A development artifact must never present itself as production evidence.
    assert artifact["citable_as_production"] is False
    assert "not a" in artifact["claim_scope"].lower()
    verify_artifact(artifact)


def test_content_hash_covers_content_not_key_order() -> None:
    first = _artifact()
    reordered = dict(reversed(list(first.items())))
    verify_artifact(reordered)
    assert first["content_hash"] == _artifact()["content_hash"]

    changed = _artifact(measurements=[{"batch": 1, "triton_ms": 0.06}])
    assert changed["content_hash"] != first["content_hash"]


def test_tampered_artifact_is_rejected() -> None:
    artifact = _artifact()
    artifact["measurements"][0]["triton_ms"] = 0.001
    with pytest.raises(UnsupportedArtifactError, match="content_hash"):
        verify_artifact(artifact)


def test_unknown_schema_version_is_rejected() -> None:
    artifact = _artifact()
    artifact["schema_version"] = max(SUPPORTED_SCHEMA_VERSIONS) + 1
    with pytest.raises(UnsupportedArtifactError, match="schema_version"):
        verify_artifact(artifact)


def test_current_schema_is_v4_and_older_versions_remain_readable() -> None:
    """Bumping the schema must not invalidate already-committed evidence."""
    assert SCHEMA_VERSION == 4
    assert SUPPORTED_SCHEMA_VERSIONS == frozenset({1, 2, 3, 4})

    # A v1 body carries no drift, tile_sweep, or baselines keys and must verify.
    v1_body = {
        "schema_version": 1,
        "suite": "gated-delta-decode-microbench",
        "target": "rtx4070-dev",
        "target_role": "development and regression",
        "citable_as_production": False,
        "claim_scope": "scope",
        "status": "pass",
        "environment": ENVIRONMENT,
        "config": {"dtype": "float16"},
        "correctness": [],
        "measurements": [],
    }
    v1_artifact = {
        **v1_body,
        "run_id": "0" * 32,
        "created_at": "2026-08-12T11:34:53Z",
        "content_hash": content_hash(v1_body),
    }
    verify_artifact(v1_artifact)

    v2_body = {**v1_body, "schema_version": 2, "drift": None, "tile_sweep": None}
    v2_artifact = {
        **v2_body,
        "run_id": "1" * 32,
        "created_at": "2026-08-12T12:12:49Z",
        "content_hash": content_hash(v2_body),
    }
    verify_artifact(v2_artifact)

    v3_body = {**v2_body, "schema_version": 3, "baselines": None}
    v3_artifact = {
        **v3_body,
        "run_id": "2" * 32,
        "created_at": "2026-08-12T12:57:21Z",
        "content_hash": content_hash(v3_body),
    }
    verify_artifact(v3_artifact)


def test_v4_records_the_profile_section() -> None:
    profile = {
        "device_limits": {"multiprocessors": 36},
        "occupancy": [{"block_v": 32, "theoretical": {"occupancy": 1.0}}],
        "bandwidth": [{"batch": 8, "fraction_of_copy_bandwidth": 0.55}],
        "compile_latency": {"cold_cache": {"first_call_ms": 1116.0}},
        "counter_metrics": {"achieved_occupancy": None},
        "counter_metrics_unavailable_reason": "ERR_NVGPUCTRPERM",
    }
    artifact = _artifact(profile=profile)
    assert artifact["schema_version"] == 4
    assert artifact["profile"] == profile
    verify_artifact(artifact)

    # Profiling results are covered by the hash like every other section.
    artifact["profile"]["bandwidth"][0]["fraction_of_copy_bandwidth"] = 0.99
    with pytest.raises(UnsupportedArtifactError, match="content_hash"):
        verify_artifact(artifact)


def test_baseline_comparison_is_recorded() -> None:
    baseline = [
        {
            "name": "flash-linear-attention:fused_recurrent_gated_delta_rule",
            "version": "0.5.2",
            "equivalence": {"within_atol": True, "output_max_abs": 3.05e-05},
            "timings": [
                {"batch": 8, "baseline_ms": 0.0264, "fabric_ms": 0.0211,
                 "fabric_speedup_vs_baseline": 1.25},
            ],
        }
    ]
    artifact = _artifact(baselines=baseline)
    assert artifact["schema_version"] == SCHEMA_VERSION
    assert artifact["baselines"] == baseline
    verify_artifact(artifact)


def test_baseline_results_are_covered_by_the_content_hash() -> None:
    artifact = _artifact(baselines=[{"name": "fla", "version": "0.5.2", "timings": []}])
    artifact["baselines"][0]["version"] = "9.9.9"
    with pytest.raises(UnsupportedArtifactError, match="content_hash"):
        verify_artifact(artifact)


def test_drift_and_tile_sweep_are_recorded() -> None:
    drift = {"steps": 1024, "output_max_abs": 6e-05, "within_single_step_atol": True}
    sweep = [{"block_v": 16, "kernel": {"registers": 40}, "timings": []}]
    artifact = _artifact(drift=drift, tile_sweep=sweep)

    assert artifact["schema_version"] == SCHEMA_VERSION
    assert artifact["drift"] == drift
    assert artifact["tile_sweep"] == sweep
    verify_artifact(artifact)


def test_absent_phases_are_recorded_as_null_not_omitted() -> None:
    """``None`` distinguishes "not run" from "ran and found nothing"."""
    artifact = _artifact()
    assert artifact["drift"] is None
    assert artifact["tile_sweep"] is None
    assert "drift" in artifact and "tile_sweep" in artifact
    verify_artifact(artifact)


def test_drift_results_are_covered_by_the_content_hash() -> None:
    baseline = _artifact(drift={"steps": 1024, "output_max_abs": 6e-05})
    tampered = dict(baseline)
    tampered["drift"] = {"steps": 1024, "output_max_abs": 1e-09}
    with pytest.raises(UnsupportedArtifactError, match="content_hash"):
        verify_artifact(tampered)


def test_declaring_the_wrong_target_fails_closed() -> None:
    """A development GPU cannot be filed as production evidence."""
    with pytest.raises(TargetMismatchError, match="T4"):
        verify_target(resolve_target("t4-production"), ENVIRONMENT)

    verify_target(resolve_target("rtx4070-dev"), ENVIRONMENT)


def test_target_requires_a_gpu() -> None:
    cpu_only = {**ENVIRONMENT, "cuda_available": False, "gpu": {"name": None}}
    with pytest.raises(TargetMismatchError, match="requires a CUDA GPU"):
        verify_target(resolve_target("rtx4070-dev"), cpu_only)


def test_unknown_target_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown target"):
        resolve_target("h100-production")


def test_write_and_read_round_trip(tmp_path: pathlib.Path) -> None:
    artifact = _artifact()
    path = write_artifact(tmp_path, artifact)

    assert path.parent.name == "rtx4070-dev"
    assert path.name.startswith("20260812T113000Z-aaaaaaaaaaaa")
    assert read_artifact(path) == artifact
    # Stored form is deterministic and human-reviewable.
    assert json.loads(path.read_text())["content_hash"] == artifact["content_hash"]


def test_dirty_worktree_is_visible_in_the_filename(tmp_path: pathlib.Path) -> None:
    dirty_environment = {**ENVIRONMENT, "git": {"commit": "b" * 40, "dirty": True}}
    path = write_artifact(tmp_path, _artifact(environment=dirty_environment))
    assert path.name.endswith("-dirty.json")
