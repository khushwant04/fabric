"""Versioned benchmark artifacts.

An artifact is the only citable form of a Fabric benchmark result. It records the
target role, the environment that produced it, the exact configuration, the
measurements, and a content hash over all of them.

A target declares which hardware role a run belongs to, and writing an artifact
fails closed when the declared target does not match the GPU actually present.
That makes it impossible to file a development-GPU measurement as production
evidence by passing the wrong flag.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import pathlib
import uuid
from typing import Any

#: Current artifact schema. v2 adds long-generation drift, compiled-kernel
#: metadata, and the value-tile sweep. v1 artifacts stay readable so previously
#: committed evidence remains verifiable.
SCHEMA_VERSION = 2
SUPPORTED_SCHEMA_VERSIONS = frozenset({1, 2})

#: Claim scope recorded on every artifact produced by the microbenchmark suite.
MICROBENCH_CLAIM_SCOPE = (
    "Single-kernel microbenchmark against the local eager reference. Not a "
    "full-model, optimized-baseline, or production performance claim."
)


@dataclasses.dataclass(frozen=True)
class Target:
    """A hardware role from the roadmap, with the GPU names it accepts."""

    name: str
    role: str
    gpu_name_contains: tuple[str, ...]
    citable_as_production: bool = False


TARGETS: dict[str, Target] = {
    "rtx4070-dev": Target(
        name="rtx4070-dev",
        role="development and regression",
        gpu_name_contains=("RTX 4070",),
    ),
    "a10-research": Target(
        name="a10-research",
        role="research evidence",
        gpu_name_contains=("A10",),
    ),
    "t4-production": Target(
        name="t4-production",
        role="production release gate",
        gpu_name_contains=("T4",),
        citable_as_production=True,
    ),
}


class TargetMismatchError(RuntimeError):
    """The declared target does not match the detected GPU."""


class UnsupportedArtifactError(ValueError):
    """The artifact schema version is not supported by this code."""


def resolve_target(name: str) -> Target:
    try:
        return TARGETS[name]
    except KeyError as exc:
        raise ValueError(
            f"unknown target {name!r}; expected one of {', '.join(sorted(TARGETS))}"
        ) from exc


def verify_target(target: Target, environment: dict[str, Any]) -> None:
    """Fail closed unless the environment's GPU belongs to the declared target."""
    gpu_name = (environment.get("gpu") or {}).get("name")
    if not gpu_name:
        raise TargetMismatchError(
            f"target {target.name!r} requires a CUDA GPU, but none was detected"
        )
    if not any(fragment in gpu_name for fragment in target.gpu_name_contains):
        raise TargetMismatchError(
            f"target {target.name!r} expects a GPU matching "
            f"{' or '.join(target.gpu_name_contains)}, but found {gpu_name!r}"
        )


def canonical_json(payload: dict[str, Any]) -> str:
    """Serialize deterministically so a hash depends on content, not key order."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def content_hash(body: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(body).encode()).hexdigest()


def build_artifact(
    *,
    target: Target,
    suite: str,
    status: str,
    environment: dict[str, Any],
    config: dict[str, Any],
    correctness: list[dict[str, Any]],
    measurements: list[dict[str, Any]],
    drift: dict[str, Any] | None = None,
    tile_sweep: list[dict[str, Any]] | None = None,
    claim_scope: str = MICROBENCH_CLAIM_SCOPE,
    run_id: str | None = None,
    created_at: dt.datetime | None = None,
) -> dict[str, Any]:
    """Assemble a complete, self-describing artifact."""
    body = {
        "schema_version": SCHEMA_VERSION,
        "suite": suite,
        "target": target.name,
        "target_role": target.role,
        "citable_as_production": target.citable_as_production,
        "claim_scope": claim_scope,
        "status": status,
        "environment": environment,
        "config": config,
        "correctness": correctness,
        "measurements": measurements,
        # ``None`` distinguishes "not run" from "ran and found nothing".
        "drift": drift,
        "tile_sweep": tile_sweep,
    }
    stamp = (created_at or dt.datetime.now(tz=dt.UTC)).astimezone(dt.UTC)
    return {
        **body,
        "run_id": run_id or uuid.uuid4().hex,
        "created_at": stamp.isoformat().replace("+00:00", "Z"),
        "content_hash": content_hash(body),
    }


def verify_artifact(artifact: dict[str, Any]) -> None:
    """Raise when an artifact is unsupported or its hash does not match."""
    version = artifact.get("schema_version")
    if version not in SUPPORTED_SCHEMA_VERSIONS:
        raise UnsupportedArtifactError(
            f"artifact schema_version {version!r} is not supported "
            f"(expected one of {sorted(SUPPORTED_SCHEMA_VERSIONS)})"
        )
    body = {
        key: value
        for key, value in artifact.items()
        if key not in {"run_id", "created_at", "content_hash"}
    }
    if content_hash(body) != artifact.get("content_hash"):
        raise UnsupportedArtifactError("artifact content_hash does not match its body")


def artifact_path(root: pathlib.Path, artifact: dict[str, Any]) -> pathlib.Path:
    """Return the versioned location for an artifact."""
    commit = (artifact["environment"].get("git") or {}).get("commit")
    short = commit[:12] if commit else "nogit"
    if (artifact["environment"].get("git") or {}).get("dirty"):
        short = f"{short}-dirty"
    stamp = artifact["created_at"].replace(":", "").replace("-", "")
    return root / artifact["target"] / f"{stamp}-{short}.json"


def write_artifact(root: pathlib.Path, artifact: dict[str, Any]) -> pathlib.Path:
    """Persist an artifact and return the path written."""
    verify_artifact(artifact)
    path = artifact_path(root, artifact)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n")
    return path


def read_artifact(path: pathlib.Path) -> dict[str, Any]:
    artifact = json.loads(path.read_text())
    verify_artifact(artifact)
    return artifact
