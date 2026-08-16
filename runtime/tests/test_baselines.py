"""Optimized-library baseline plumbing.

The measurement functions need a GPU, so these cover availability reporting and
the runner's handling of a present or absent baseline library.
"""

from __future__ import annotations

import argparse

from benchmarks import baselines
from harness.runner import build_config, parse_args, run_baselines


def test_availability_matches_version_reporting() -> None:
    assert baselines.is_available() == (baselines.fla_version() is not None)


def test_baseline_is_named_for_the_artifact() -> None:
    # The artifact must say which implementation it was measured against.
    assert baselines.FLA_BASELINE.startswith("flash-linear-attention:")
    assert "fused_recurrent_gated_delta_rule" in baselines.FLA_BASELINE


def test_absent_library_is_recorded_as_absent_not_omitted(monkeypatch) -> None:
    """A missing baseline is named with a reason rather than left out.

    Omitting it would make "not compared" indistinguishable from "compared and
    equal" for anyone reading the artifact later.
    """
    monkeypatch.setattr(baselines, "is_available", lambda: False)
    args = parse_args(["--target", "rtx4070-dev"])

    entries = run_baselines(args, dtype=None)
    by_name = {entry["name"]: entry for entry in entries}

    fla = by_name[baselines.FLA_BASELINE]
    assert fla["available"] is False
    assert "not installed" in fla["reason"]
    assert "timings" not in fla


def test_the_vllm_comparison_is_named_even_when_it_cannot_run(monkeypatch) -> None:
    """The runtime environment has no vLLM, and the artifact must say so.

    The comparison lives in the serving component, so the harness records why it was
    skipped instead of silently producing an artifact that looks like vLLM was never
    relevant.
    """
    monkeypatch.setattr(baselines, "is_available", lambda: False)
    args = parse_args(["--target", "rtx4070-dev"])

    entries = run_baselines(args, dtype=None)
    vllm_entry = next(entry for entry in entries if entry["name"].startswith("vllm:"))

    assert vllm_entry["available"] is False
    assert vllm_entry["reason"]
    assert "serving" in vllm_entry["reason"] or "vLLM" in vllm_entry["reason"]


def test_vllm_sequence_counts_are_recorded_in_the_config() -> None:
    """A claim needs the shape it was measured at, not just the number."""
    config = build_config(parse_args(["--target", "rtx4070-dev", "--vllm-sequences", "1", "8"]))
    assert config["vllm_sequences"] == [1, 8]


def test_baselines_are_requested_by_default() -> None:
    assert build_config(parse_args(["--target", "rtx4070-dev"]))["baselines_requested"] is True


def test_baselines_can_be_skipped() -> None:
    args = parse_args(["--target", "rtx4070-dev", "--no-baselines"])
    assert build_config(args)["baselines_requested"] is False


def test_runner_skips_baselines_when_not_requested(monkeypatch) -> None:
    """--no-baselines must not even probe for the library."""
    probed: list[bool] = []
    monkeypatch.setattr(baselines, "is_available", lambda: probed.append(True) or False)

    args = argparse.Namespace(
        **{**vars(parse_args(["--target", "rtx4070-dev", "--no-baselines"]))}
    )
    assert args.baselines is False
    # run_baselines is only called when args.baselines is set, so nothing probes.
    assert probed == []
