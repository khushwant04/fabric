"""Artifact schema, hashing, and target-hardware guard."""

from __future__ import annotations

import datetime as dt
import json
import pathlib

import pytest

from harness.artifact import (
    SCHEMA_VERSION,
    TargetMismatchError,
    UnsupportedArtifactError,
    build_artifact,
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
    artifact["schema_version"] = SCHEMA_VERSION + 1
    with pytest.raises(UnsupportedArtifactError, match="schema_version"):
        verify_artifact(artifact)


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
