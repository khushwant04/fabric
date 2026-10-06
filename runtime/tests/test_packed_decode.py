"""Packed decode correctness across grouped heads, dtype, slots, and launch tiles."""

from __future__ import annotations

import pytest
import torch

from kernels.gated_delta_packed_decode import (
    fused_packed_decode,
    torch_packed_decode_reference,
    validate_packed_inputs,
)

requires_cuda = pytest.mark.skipif(not torch.cuda.is_available(), reason="needs a CUDA GPU")


def make_inputs(device="cpu", *, heads=2, value_heads=4, key=16, value=24, dtype=torch.float16):
    generator = torch.Generator(device=device).manual_seed(41)
    batch = 4
    packed = torch.randn(
        batch,
        2 * heads * key + value_heads * value,
        device=device,
        dtype=dtype,
        generator=generator,
    )
    a = torch.randn(batch, value_heads, device=device, dtype=dtype, generator=generator)
    b = torch.randn(batch, value_heads, device=device, dtype=dtype, generator=generator)
    log = torch.randn(value_heads, device=device, generator=generator)
    bias = torch.randn(value_heads, device=device, generator=generator)
    state = torch.randn(9, value_heads, value, key, device=device, generator=generator) * 0.1
    out = torch.full((batch, 1, value_heads, value), float("nan"), device=device, dtype=dtype)
    indices = torch.tensor([7, 0, 2, -1], device=device, dtype=torch.int64)
    return [packed, a, b, log, bias, key**-0.5, state, out, indices, True]


def test_reference_rounds_gate_like_vllm_and_preserves_unaddressed_slots():
    inputs = make_inputs(heads=1, value_heads=1, key=1, value=1)
    inputs[0].fill_(1)
    inputs[0][:, 1] = 1
    inputs[1].zero_()
    inputs[2].fill_(0.123)
    inputs[3].zero_()
    inputs[4].zero_()
    inputs[6].zero_()
    original = inputs[6].clone()
    inputs[9] = False
    out, state = torch_packed_decode_reference(*inputs)
    strength = torch.sigmoid(inputs[2].float()).half().float()[0, 0]
    assert not torch.equal(strength, torch.sigmoid(inputs[2].float())[0, 0])
    torch.testing.assert_close(state[7, 0, 0, 0], strength, rtol=0, atol=0)
    torch.testing.assert_close(state[2, 0, 0, 0], strength, rtol=0, atol=0)
    torch.testing.assert_close(
        state[[0, 1, 3, 4, 5, 6, 8]], original[[0, 1, 3, 4, 5, 6, 8]], rtol=0, atol=0
    )
    assert torch.count_nonzero(out[[1, 3]]) == 0
    assert out.data_ptr() == inputs[7].data_ptr()
    assert state.data_ptr() == inputs[6].data_ptr()


@pytest.mark.parametrize("indices", [[9, 0, 2, -1], [7, 7, 2, -1]])
def test_reference_rejects_out_of_range_or_duplicate_active_slots(indices):
    inputs = make_inputs()
    inputs[8] = torch.tensor(indices)
    original = inputs[6].clone()
    with pytest.raises(ValueError, match="unique and in bounds"):
        torch_packed_decode_reference(*inputs)
    torch.testing.assert_close(inputs[6], original, rtol=0, atol=0)


def test_metadata_allows_outer_slot_padding_but_rejects_inner_strides():
    inputs = make_inputs()
    state = inputs[6]
    padded = torch.zeros(state.shape[0] * 2, *state.shape[1:])[::2]
    padded.copy_(state)
    inputs[6] = padded
    assert validate_packed_inputs(*inputs[:9]) == (4, 2, 4, 16, 24)
    inputs[6] = torch.zeros(9, 4, 16, 24).transpose(-1, -2)
    with pytest.raises(ValueError, match="dimensions must be contiguous"):
        validate_packed_inputs(*inputs[:9])


@pytest.mark.parametrize(
    "change, match",
    [
        (lambda inputs: inputs.__setitem__(8, inputs[8].float()), "int32 or int64"),
        (lambda inputs: inputs.__setitem__(6, inputs[6].half()), "FP32"),
        (lambda inputs: inputs.__setitem__(1, torch.zeros(4, 8)[:, ::2]), "contiguous heads"),
        (lambda inputs: inputs.__setitem__(3, torch.zeros(8)[::2]), "must be contiguous"),
        (lambda inputs: inputs.__setitem__(5, float("nan")), "finite and positive"),
        (lambda inputs: inputs.__setitem__(0, inputs[0][:, :-1]), "packed width"),
    ],
)
def test_unsafe_metadata_is_rejected_before_launch(change, match):
    inputs = make_inputs()
    original = inputs[6].clone()
    change(inputs)
    with pytest.raises(ValueError, match=match):
        fused_packed_decode(*inputs)
    torch.testing.assert_close(inputs[6].float(), original, rtol=1e-3, atol=1e-3)


@requires_cuda
@pytest.mark.parametrize("dtype", [torch.float16, torch.bfloat16, torch.float32])
@pytest.mark.parametrize("tile,warps", [(16, 1), (32, 1), (64, 2), (128, 4)])
def test_fused_grouped_heads_matches_independent_reference(dtype, tile, warps):
    inputs = make_inputs("cuda", dtype=dtype)
    expected = [value.clone() if isinstance(value, torch.Tensor) else value for value in inputs]
    expected_out, expected_state = torch_packed_decode_reference(*expected)
    actual_out, actual_state = fused_packed_decode(*inputs, block_v=tile, num_warps=warps)
    torch.cuda.synchronize()
    tolerance = 2e-2 if dtype == torch.bfloat16 else 2e-3
    torch.testing.assert_close(actual_out, expected_out, rtol=tolerance, atol=tolerance)
    torch.testing.assert_close(actual_state, expected_state, rtol=2e-4, atol=2e-4)


@requires_cuda
@pytest.mark.parametrize("value_heads", [16, 32])
def test_qwen_launch_geometries_remain_correct_across_decode_steps(value_heads):
    inputs = make_inputs("cuda", heads=16, value_heads=value_heads, key=128, value=128)
    expected = [value.clone() if isinstance(value, torch.Tensor) else value for value in inputs]
    for _ in range(32):
        expected_out, expected_state = torch_packed_decode_reference(*expected)
        actual_out, actual_state = fused_packed_decode(*inputs)
    torch.cuda.synchronize()
    torch.testing.assert_close(actual_out, expected_out, rtol=2e-3, atol=2e-3)
    torch.testing.assert_close(actual_state, expected_state, rtol=2e-3, atol=2e-3)


@requires_cuda
def test_fused_decode_is_cuda_graph_capturable():
    inputs = make_inputs("cuda")
    expected = [value.clone() if isinstance(value, torch.Tensor) else value for value in inputs]
    side = torch.cuda.Stream()
    side.wait_stream(torch.cuda.current_stream())
    with torch.cuda.stream(side):
        for _ in range(3):
            fused_packed_decode(*inputs)
    torch.cuda.current_stream().wait_stream(side)
    torch.cuda.synchronize()
    inputs[6].copy_(expected[6])
    graph = torch.cuda.CUDAGraph()
    with torch.cuda.graph(graph):
        fused_packed_decode(*inputs)
    inputs[6].copy_(expected[6])
    graph.replay()
    expected_out, expected_state = torch_packed_decode_reference(*expected)
    torch.cuda.synchronize()
    torch.testing.assert_close(inputs[7], expected_out, rtol=2e-3, atol=2e-3)
    torch.testing.assert_close(inputs[6], expected_state, rtol=2e-3, atol=2e-3)
