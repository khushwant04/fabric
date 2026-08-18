"""Compare the Fabric fused packed-decode kernel against vLLM's own.

Both kernels are given the same inputs and the same starting state, and both are asked to
produce the output and the next state. Agreement is checked on both, because a kernel that
returns the right answer while corrupting the state is wrong in a way a single-step check
would not notice.

Timing is reported for the default tile, which is vLLM's, and for a sweep of tiles, since
the best shape belongs to the GPU and the batch rather than to the algorithm.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys

import torch

sys.path.insert(0, "../runtime")

from kernels.gated_delta_packed_decode import fused_packed_decode  # noqa: E402

from fabric_serving.vllm_ops import resolve  # noqa: E402

# The launch model's own linear-attention shapes. Measuring at anything else describes a
# model nobody is serving.
LAUNCH_MODEL = {"H": 16, "HV": 16, "K": 128, "V": 128}


def make_inputs(batch: int, shapes: dict[str, int], dtype: torch.dtype, seed: int = 0):
    torch.manual_seed(seed)
    H, HV, K, V = shapes["H"], shapes["HV"], shapes["K"], shapes["V"]
    device = "cuda"

    mixed_qkv = torch.randn(batch, 2 * H * K + HV * V, device=device, dtype=dtype)
    a = torch.randn(batch, HV, device=device, dtype=torch.float32)
    b = torch.randn(batch, HV, device=device, dtype=torch.float32)
    A_log = torch.randn(HV, device=device, dtype=torch.float32)
    dt_bias = torch.randn(HV, device=device, dtype=torch.float32)
    # Slot 0 is reserved for "no state", so real sequences start at 1.
    slots = batch + 1
    state = torch.randn(slots, HV, V, K, device=device, dtype=torch.float32) * 0.1
    indices = torch.arange(1, batch + 1, device=device, dtype=torch.int32)
    return {
        "mixed_qkv": mixed_qkv,
        "a": a,
        "b": b,
        "A_log": A_log,
        "dt_bias": dt_bias,
        "scale": float(K) ** -0.5,
        "state": state,
        "indices": indices,
        "shapes": (HV, V),
    }


def _run(fn, inputs, dtype, **kwargs):
    HV, V = inputs["shapes"]
    batch = inputs["mixed_qkv"].shape[0]
    state = inputs["state"].clone()
    out = torch.empty(batch, 1, HV, V, device="cuda", dtype=dtype)
    fn(
        inputs["mixed_qkv"],
        inputs["a"],
        inputs["b"],
        inputs["A_log"],
        inputs["dt_bias"],
        inputs["scale"],
        state,
        out,
        inputs["indices"],
        True,
        **kwargs,
    )
    return out, state


def _sampler(fn, inputs, dtype, **kwargs):
    """Return a callable producing one timing sample, in microseconds per launch.

    Buffers are allocated once. Launches are timed in runs rather than singly: a lone
    launch spends most of its window waiting for the host to enqueue it, including the
    argument checking both wrappers do, so timing one measures the caller as much as the
    kernel.
    """
    HV, V = inputs["shapes"]
    batch = inputs["mixed_qkv"].shape[0]
    state = inputs["state"].clone()
    out = torch.empty(batch, 1, HV, V, device="cuda", dtype=dtype)
    inner = 20

    def once():
        fn(
            inputs["mixed_qkv"], inputs["a"], inputs["b"], inputs["A_log"],
            inputs["dt_bias"], inputs["scale"], state, out, inputs["indices"], True,
            **kwargs,
        )

    def sample() -> float:
        # Restored between runs so drift cannot push the state into denormals and time
        # arithmetic the model would never perform.
        state.copy_(inputs["state"])
        torch.cuda.synchronize()
        first = torch.cuda.Event(enable_timing=True)
        last = torch.cuda.Event(enable_timing=True)
        first.record()
        for _ in range(inner):
            once()
        last.record()
        torch.cuda.synchronize()
        return first.elapsed_time(last) * 1000.0 / inner

    for _ in range(3):
        sample()
    return sample


def compare_interleaved(candidates: dict, inputs, dtype, rounds: int) -> dict[str, float]:
    """Time several implementations by alternating between them.

    Measured one after the other, a GPU that heats up or is shared reports the second as
    slower for reasons that have nothing to do with the code: a first pass on a laptop had
    vLLM slower at eight sequences than at sixteen, which cannot be true. Alternating
    spreads any drift across all of them equally.
    """
    samplers = {name: _sampler(fn, inputs, dtype, **kw) for name, (fn, kw) in candidates.items()}
    collected: dict[str, list[float]] = {name: [] for name in samplers}
    for _ in range(rounds):
        for name, sample in samplers.items():
            collected[name].append(sample())
    return {name: statistics.median(values) for name, values in collected.items()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch", type=int, nargs="+", default=[1, 8, 16, 32])
    parser.add_argument("--dtype", default="float16", choices=("float16", "bfloat16", "float32"))
    parser.add_argument("--warmup", type=int, default=30)
    parser.add_argument("--reps", type=int, default=200)
    parser.add_argument("--sweep", action="store_true", help="also try other tile shapes")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if not torch.cuda.is_available():
        print("no CUDA device", file=sys.stderr)
        return 2

    ops = resolve()
    if not ops.has_packed_decode:
        print(f"this vLLM ({ops.version}) has no packed decode op", file=sys.stderr)
        return 2

    dtype = getattr(torch, args.dtype)
    device_name = torch.cuda.get_device_name(0)
    capability = ".".join(str(c) for c in torch.cuda.get_device_capability(0))
    results = []

    for batch in args.batch:
        inputs = make_inputs(batch, LAUNCH_MODEL, dtype)

        vllm_out, vllm_state = _run(ops.packed, inputs, dtype)
        fabric_out, fabric_state = _run(fused_packed_decode, inputs, dtype)

        # Both outputs and both next states must agree: a kernel that answers correctly
        # while corrupting the state fails on the following token, not this one.
        out_delta = (fabric_out.float() - vllm_out.float()).abs().max().item()
        state_delta = (fabric_state - vllm_state).abs().max().item()

        rounds = max(3, args.reps // 20)
        timings = compare_interleaved(
            {"vllm": (ops.packed, {}), "fabric": (fused_packed_decode, {})},
            inputs, dtype, rounds,
        )
        vllm_us, fabric_us = timings["vllm"], timings["fabric"]

        record = {
            "batch": batch,
            "max_output_difference": out_delta,
            "max_state_difference": state_delta,
            "vllm_us": round(vllm_us, 2),
            "fabric_us": round(fabric_us, 2),
            "speedup": round(vllm_us / fabric_us, 3),
        }

        if args.sweep:
            tiles = {}
            for block_v in (16, 32, 64, 128):
                for warps in (1, 2, 4, 8):
                    if warps * 32 > block_v * 4:
                        continue
                    tiles[f"{block_v}/{warps}"] = (
                        fused_packed_decode,
                        {"block_v": block_v, "num_warps": warps},
                    )
            # vLLM is measured alongside the candidates, so the ratio comes from timings
            # taken under the same conditions rather than from an earlier pass.
            tiles["vllm"] = (ops.packed, {})
            swept = compare_interleaved(tiles, inputs, dtype, max(3, rounds // 2))
            reference = swept.pop("vllm")
            best = None
            for label, us in swept.items():
                if best is None or us < best[0]:
                    block_v, warps = label.split("/")
                    best = (us, int(block_v), int(warps))
            if best is not None:
                record["sweep_vllm_us"] = round(reference, 2)
            if best is not None:
                record["best_us"] = round(best[0], 2)
                record["best_block_v"] = best[1]
                record["best_num_warps"] = best[2]
                record["best_speedup"] = round(reference / best[0], 3)

        results.append(record)

    payload = {
        "device": device_name,
        "compute_capability": capability,
        "vllm_version": ops.version,
        "vllm_module": ops.module,
        "dtype": args.dtype,
        "shapes": LAUNCH_MODEL,
        "results": results,
    }

    if args.json:
        print(json.dumps(payload, indent=2))
        return 0

    print(f"{device_name} (sm{capability}), vLLM {ops.version}, {args.dtype}")
    print(f"shapes: {LAUNCH_MODEL}")
    header = f"{'batch':>6} {'vllm us':>9} {'fabric us':>10} {'speedup':>8} {'out delta':>11} {'state delta':>12}"
    if args.sweep:
        header += f" {'best us':>9} {'tile':>10} {'best x':>7}"
    print(header)
    for r in results:
        line = (
            f"{r['batch']:>6} {r['vllm_us']:>9.2f} {r['fabric_us']:>10.2f} "
            f"{r['speedup']:>8.3f} {r['max_output_difference']:>11.2e} {r['max_state_difference']:>12.2e}"
        )
        if "best_us" in r:
            tile = "{}/{}w".format(r["best_block_v"], r["best_num_warps"])
            line += f" {r['best_us']:>9.2f} {tile:>10} {r['best_speedup']:>7.3f}"
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
