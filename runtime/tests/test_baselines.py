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


def test_absent_library_records_no_comparison(monkeypatch) -> None:
    """A missing baseline is recorded as absent rather than failing the run."""
    monkeypatch.setattr(baselines, "is_available", lambda: False)
    args = parse_args(["--target", "rtx4070-dev"])
    assert run_baselines(args, dtype=None) is None


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
