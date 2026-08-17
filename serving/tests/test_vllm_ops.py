"""Finding vLLM's gated-delta op, and refusing to compare against a different function.

The op's module path moved between the version this project pinned and the version that
can serve the launch model. Hard-coding either path meant the comparison silently became
unrunnable when the version changed, which is part of why the substitution ended up
unregistered in the live host without that being visible.
"""

from __future__ import annotations

import pytest

from fabric_serving import vllm_ops


def test_the_op_is_found_wherever_this_version_keeps_it() -> None:
    ops = vllm_ops.resolve()
    if ops is None:
        pytest.skip("vLLM is not installed in this environment")

    assert ops.module in vllm_ops._CANDIDATE_MODULES
    assert callable(ops.unfused)
    # The version is recorded so a measurement can be attributed to it.
    assert ops.version


def test_the_newest_layout_is_searched_first() -> None:
    """A version providing both paths must resolve to its current one."""
    assert vllm_ops._CANDIDATE_MODULES[0].startswith("vllm.third_party")


def test_resolution_returns_none_rather_than_raising(monkeypatch) -> None:
    """The harness records an unavailable baseline; it must not fail the run."""
    monkeypatch.setattr(vllm_ops, "_CANDIDATE_MODULES", ("vllm.does.not.exist",))
    assert vllm_ops.resolve() is None


def test_packed_decode_presence_is_reported_not_assumed() -> None:
    ops = vllm_ops.resolve()
    if ops is None:
        pytest.skip("vLLM is not installed in this environment")
    # A fact about the installed version, either way.
    assert isinstance(ops.has_packed_decode, bool)
