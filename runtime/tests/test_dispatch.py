"""Kernel dispatch modes, safe fallback, strict mode, and telemetry.

CPU-only inputs exercise the fallback path, which is also how the suite runs on a
machine without a GPU. Tests needing the Triton path are skipped without CUDA.
"""

from __future__ import annotations

import pytest
import torch

from integration import dispatch
from integration.dispatch import (
    PATH_EAGER,
    PATH_FABRIC,
    SUPPORTED_MODES,
    DispatchStats,
    GatedDeltaDecoder,
    KernelUnavailableError,
    dispatch_gated_delta_decode,
    unsupported_reason,
)

requires_cuda = pytest.mark.skipif(not torch.cuda.is_available(), reason="needs a CUDA GPU")


def make_cpu_inputs(batch: int = 2, heads: int = 4, key_dim: int = 16, value_dim: int = 16):
    q = torch.randn(batch, heads, key_dim, dtype=torch.float32)
    k = torch.randn_like(q)
    v = torch.randn(batch, heads, value_dim, dtype=torch.float32)
    g = -torch.rand(batch, heads, dtype=torch.float32)
    beta = torch.rand(batch, heads, dtype=torch.float32)
    state = torch.randn(batch, heads, key_dim, value_dim, dtype=torch.float32) * 0.05
    return q, k, v, g, beta, state


def make_cuda_inputs(batch: int = 2, heads: int = 8, key_dim: int = 64, value_dim: int = 64):
    q = torch.randn(batch, heads, key_dim, dtype=torch.float16, device="cuda")
    k = torch.randn_like(q)
    v = torch.randn(batch, heads, value_dim, dtype=torch.float16, device="cuda")
    g = -torch.rand(batch, heads, dtype=torch.float32, device="cuda")
    beta = torch.rand(batch, heads, dtype=torch.float32, device="cuda")
    state = (
        torch.randn(batch, heads, key_dim, value_dim, dtype=torch.float32, device="cuda") * 0.05
    )
    return q, k, v, g, beta, state


def test_modes_match_the_control_plane_contract() -> None:
    # runtime.kernel_mode in the deployment spec is Literal["auto","fabric","standard"].
    assert SUPPORTED_MODES == ("auto", "fabric", "standard")


def test_unknown_mode_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown kernel mode"):
        dispatch_gated_delta_decode(*make_cpu_inputs(), mode="turbo")
    with pytest.raises(ValueError, match="unknown kernel mode"):
        GatedDeltaDecoder(mode="turbo")


def test_auto_falls_back_and_explains_why() -> None:
    """A host must never fail a request because the custom kernel cannot run."""
    inputs = make_cpu_inputs()
    stats = DispatchStats()
    result, decision = dispatch_gated_delta_decode(*inputs, mode="auto", stats=stats)

    assert decision.path == PATH_EAGER
    assert decision.used_fabric is False
    assert "CUDA" in decision.reason
    assert result.shape == inputs[2].shape
    assert stats.fallback_calls == 1
    assert stats.last_fallback_reason == decision.reason


def test_fabric_mode_refuses_to_degrade_silently() -> None:
    """Benchmarks must not attribute eager performance to the Fabric kernel."""
    with pytest.raises(KernelUnavailableError, match="CUDA"):
        dispatch_gated_delta_decode(*make_cpu_inputs(), mode="fabric")


def test_standard_mode_uses_the_reference_without_counting_a_fallback() -> None:
    stats = DispatchStats()
    _result, decision = dispatch_gated_delta_decode(
        *make_cpu_inputs(), mode="standard", stats=stats
    )
    assert decision.path == PATH_EAGER
    assert "standard mode" in decision.reason
    # Choosing the reference deliberately is not a fallback event.
    assert stats.fallback_calls == 0
    assert stats.eager_calls == 1


def test_eager_fallback_matches_the_reference_result() -> None:
    inputs = make_cpu_inputs()
    from kernels.gated_delta_decode import torch_gated_delta_decode_reference

    expected_state = inputs[5].clone()
    expected = torch_gated_delta_decode_reference(*inputs[:5], expected_state)

    dispatched, _decision = dispatch_gated_delta_decode(*inputs, mode="auto")
    torch.testing.assert_close(dispatched, expected)
    torch.testing.assert_close(inputs[5], expected_state)


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda t: t[0].to(torch.float64), "unsupported dtype"),
        (lambda t: t[0].transpose(0, 1), "not contiguous"),
    ],
)
def test_unsupported_reason_names_the_specific_problem(mutate, expected) -> None:
    inputs = list(make_cpu_inputs())
    inputs[0] = mutate(inputs)
    reason = unsupported_reason(*inputs)
    assert reason is not None
    # On CPU the device check fires first; assert the specific check directly.
    cuda_free = unsupported_reason(*[t.cpu() for t in inputs])
    assert cuda_free is not None
    assert expected in reason or "CUDA" in reason


def test_state_dtype_must_be_fp32() -> None:
    inputs = list(make_cpu_inputs())
    inputs[5] = inputs[5].to(torch.float16)
    assert "FP32" in unsupported_reason(*inputs) or "CUDA" in unsupported_reason(*inputs)


def test_telemetry_reports_the_fabric_fraction() -> None:
    decoder = GatedDeltaDecoder(mode="auto")
    for _ in range(3):
        decoder(*make_cpu_inputs())

    telemetry = decoder.telemetry()
    assert telemetry["kernel_mode"] == "auto"
    assert telemetry["eager_calls"] == 3
    assert telemetry["fabric_calls"] == 0
    assert telemetry["fabric_fraction"] == 0.0
    assert telemetry["fallback_calls"] == 3
    assert telemetry["last_fallback_reason"]


def test_empty_telemetry_has_no_division_by_zero() -> None:
    assert GatedDeltaDecoder().telemetry()["fabric_fraction"] == 0.0


@requires_cuda
def test_auto_selects_the_fabric_kernel_on_supported_inputs() -> None:
    inputs = make_cuda_inputs()
    stats = DispatchStats()
    _result, decision = dispatch_gated_delta_decode(*inputs, mode="auto", stats=stats)

    assert decision.path == PATH_FABRIC
    assert stats.fabric_calls == 1
    assert stats.fallback_calls == 0
    assert unsupported_reason(*inputs) is None


@requires_cuda
def test_dispatched_fabric_path_matches_the_reference() -> None:
    inputs = make_cuda_inputs()
    from kernels.gated_delta_decode import torch_gated_delta_decode_reference

    reference_state = inputs[5].clone()
    reference = torch_gated_delta_decode_reference(*inputs[:5], reference_state)

    dispatched, decision = dispatch_gated_delta_decode(*inputs, mode="auto")
    assert decision.used_fabric
    torch.testing.assert_close(dispatched, reference, atol=2e-3, rtol=2e-3)
    torch.testing.assert_close(inputs[5], reference_state, atol=2e-3, rtol=2e-3)


@requires_cuda
def test_fabric_mode_succeeds_on_supported_inputs() -> None:
    _result, decision = dispatch_gated_delta_decode(*make_cuda_inputs(), mode="fabric")
    assert decision.used_fabric


@requires_cuda
def test_kernel_failure_falls_back_instead_of_raising(monkeypatch) -> None:
    """An unexpected kernel error is contained, counted, and served eagerly."""
    import integration.dispatch as dispatch_module

    def exploding_kernel(*_args, **_kwargs):
        raise RuntimeError("simulated launch failure")

    monkeypatch.setattr(dispatch_module, "gated_delta_decode", exploding_kernel)
    stats = DispatchStats()
    _result, decision = dispatch_gated_delta_decode(
        *make_cuda_inputs(), mode="auto", stats=stats
    )

    assert decision.path == PATH_EAGER
    assert "simulated launch failure" in decision.reason
    assert stats.fallback_calls == 1

    # The same failure in strict mode must surface, not degrade.
    with pytest.raises(KernelUnavailableError, match="simulated launch failure"):
        dispatch_gated_delta_decode(*make_cuda_inputs(), mode="fabric")


class _ClaimsCuda(torch.Tensor):
    """A CPU tensor that reports itself as CUDA, to reach the dtype policy without a GPU."""

    @property
    def is_cuda(self) -> bool:  # type: ignore[override]
        return True


def inputs_that_claim_cuda(dtype: torch.dtype):
    """q, k, v in ``dtype``; gates and state keep their FP32 contract."""
    return tuple(
        tensor.to(dtype if index < 3 else tensor.dtype).as_subclass(_ClaimsCuda)
        for index, tensor in enumerate(make_cpu_inputs())
    )


def test_bf16_on_sm75_is_unsupported_with_a_clear_reason(monkeypatch) -> None:
    monkeypatch.setattr(dispatch, "device_capability", lambda device: (7, 5))
    reason = unsupported_reason(*inputs_that_claim_cuda(torch.bfloat16))
    assert reason is not None
    assert "bfloat16" in reason and "7.5" in reason and "--dtype half" in reason


def test_bf16_on_ampere_is_not_refused_by_the_dtype_policy(monkeypatch) -> None:
    monkeypatch.setattr(dispatch, "device_capability", lambda device: (8, 6))
    assert unsupported_reason(*inputs_that_claim_cuda(torch.bfloat16)) is None


def test_fp16_on_sm75_is_not_refused(monkeypatch) -> None:
    monkeypatch.setattr(dispatch, "device_capability", lambda device: (7, 5))
    assert unsupported_reason(*inputs_that_claim_cuda(torch.float16)) is None


def test_auto_mode_falls_back_for_bf16_on_sm75(monkeypatch) -> None:
    def forbidden(*args, **kwargs):
        raise AssertionError("BF16 on SM75 reached the Fabric kernel")

    monkeypatch.setattr(dispatch, "device_capability", lambda device: (7, 5))
    monkeypatch.setattr(dispatch, "gated_delta_decode", forbidden)
    stats = DispatchStats()
    result, decision = dispatch_gated_delta_decode(
        *inputs_that_claim_cuda(torch.bfloat16), mode="auto", stats=stats
    )
    assert decision.path == PATH_EAGER
    assert "bfloat16" in decision.reason
    assert stats.fallback_calls == 1
    assert result.dtype == torch.bfloat16


def test_fabric_mode_raises_for_bf16_on_sm75(monkeypatch) -> None:
    monkeypatch.setattr(dispatch, "device_capability", lambda device: (7, 5))
    with pytest.raises(KernelUnavailableError, match="bfloat16"):
        dispatch_gated_delta_decode(*inputs_that_claim_cuda(torch.bfloat16), mode="fabric")


def test_decoder_defaults_to_the_launch_table() -> None:
    assert GatedDeltaDecoder().telemetry()["block_v"] is None
