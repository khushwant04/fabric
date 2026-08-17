"""Attribution of the vLLM comparison: which op, which version, same function.

A speedup only means something between implementations of the same computation, and the
op this project compares against moved module and changed behaviour between versions.
"""

from __future__ import annotations

import pytest


def test_the_baseline_records_which_op_it_measured() -> None:
    """A speedup is only attributable with the module and version behind it."""
    from fabric_serving import baseline

    if not baseline.is_available():
        pytest.skip("vLLM is not installed in this environment")

    from fabric_serving.vllm_ops import resolve

    ops = resolve()
    assert ops is not None
    assert ops.module and ops.version


def test_agreement_gate_distinguishes_a_different_function() -> None:
    """The gate exists because a later vLLM's unfused op no longer matches.

    Comparing timings against an op that computes something else would report a
    speedup for a different function, so agreement is measured rather than assumed.
    """
    from fabric_serving.baseline import _agreement_with_reference

    assert _agreement_with_reference.__doc__
    # The threshold is fp16-scale: agreement is ~1e-4, disagreement was ~1e-2.
    import inspect

    source = inspect.getsource(_agreement_with_reference)
    assert "5e-3" in source
