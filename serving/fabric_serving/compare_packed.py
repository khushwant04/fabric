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
import hashlib
import json
import math
import pathlib
import statistics
import sys
from typing import Any

import torch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "runtime"))

from harness import environment  # noqa: E402
from harness.artifact import (  # noqa: E402
    artifact_path,
    build_artifact,
    resolve_target,
    verify_artifact,
    verify_target,
)
from kernels.gated_delta_packed_decode import fused_packed_decode  # noqa: E402

from fabric_serving.vllm_ops import resolve  # noqa: E402

# Backwards-compatible launch shape, not inferred metadata for an arbitrary model.
LAUNCH_MODEL = {"H": 16, "HV": 16, "K": 128, "V": 128}
_GATED_DELTA_TYPES = frozenset(
    {
        "qwen3_5",
        "qwen3_5_text",
        "qwen3_5_moe",
        "qwen3_5_moe_text",
        "qwen3_next",
    }
)
_SHAPE_FIELDS = {
    "H": "linear_num_key_heads",
    "HV": "linear_num_value_heads",
    "K": "linear_key_head_dim",
    "V": "linear_value_head_dim",
}
_CLAIM_SCOPE = (
    "Synthetic packed gated-delta single-kernel comparison against the resolved stock vLLM op. "
    "Not a full-model, generation-quality, or end-to-end serving performance claim."
)

#: Set by the command line before any sampler is built.
_USE_GRAPH = False
_WARMUP = 30


def load_model_config(path: pathlib.Path | None) -> tuple[dict[str, int], dict[str, Any]]:
    """Read explicit gated-delta dimensions; never derive heads from model names."""
    if path is None:
        return dict(LAUNCH_MODEL), {"shape_source": "legacy_launch_shape", "model_type": None}
    raw = path.read_bytes()
    config = json.loads(raw)
    if not isinstance(config, dict):
        raise ValueError("model config must be a JSON object")
    text = config.get("text_config", config)
    if not isinstance(text, dict):
        raise ValueError("text_config must be a JSON object")
    model_type = text.get("model_type", config.get("model_type"))
    if not isinstance(model_type, str) or model_type not in _GATED_DELTA_TYPES:
        raise ValueError(f"unsupported gated-delta model_type: {model_type!r}")
    shapes = {}
    for name, field in _SHAPE_FIELDS.items():
        value = text.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{field} must be an explicit positive integer")
        shapes[name] = value
    if shapes["HV"] % shapes["H"]:
        raise ValueError("linear_num_value_heads must be divisible by linear_num_key_heads")
    return shapes, {
        "shape_source": "model_config",
        "model_type": model_type,
        "model_config_path": str(path.resolve()),
        "model_config_sha256": hashlib.sha256(raw).hexdigest(),
    }


def make_inputs(
    batch: int,
    shapes: dict[str, int],
    dtype: torch.dtype,
    seed: int = 0,
    *,
    device: str = "cuda",
):
    generator = torch.Generator(device=device).manual_seed(seed)
    H, HV, K, V = shapes["H"], shapes["HV"], shapes["K"], shapes["V"]
    mixed_qkv = torch.randn(
        batch, 2 * H * K + HV * V, device=device, dtype=dtype, generator=generator
    )
    # a/b are projection outputs in the model dtype. Stock vLLM rounds sigmoid(b)
    # to b's dtype, so FP32 gates would miss the real FP16/BF16 correctness contract.
    a = torch.randn(batch, HV, device=device, dtype=dtype, generator=generator)
    b = torch.randn(batch, HV, device=device, dtype=dtype, generator=generator)
    A_log = torch.randn(HV, device=device, dtype=torch.float32, generator=generator)
    dt_bias = torch.randn(HV, device=device, dtype=torch.float32, generator=generator)
    # Slot 0 is reserved for "no state", so real sequences start at 1.
    slots = batch + 3
    state = (
        torch.randn(slots, HV, V, K, device=device, dtype=torch.float32, generator=generator) * 0.1
    )
    indices = torch.randperm(slots - 1, device=device, generator=generator)[:batch] + 1
    return {
        "mixed_qkv": mixed_qkv,
        "a": a,
        "b": b,
        "A_log": A_log,
        "dt_bias": dt_bias,
        "scale": float(K) ** -0.5,
        "state": state,
        "indices": indices.to(torch.int32).contiguous(),
        "shapes": (HV, V),
    }


def _run(fn, inputs, dtype, *, state=None, **kwargs):
    HV, V = inputs["shapes"]
    batch = inputs["mixed_qkv"].shape[0]
    state = inputs["state"].clone() if state is None else state
    out = torch.empty(batch, 1, HV, V, device=state.device, dtype=dtype)
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


def _difference(actual: torch.Tensor, expected: torch.Tensor) -> float | None:
    delta = (actual.float() - expected.float()).abs().max().item()
    return float(delta) if math.isfinite(delta) else None


def check_candidate(
    stock,
    candidate,
    batch: int,
    shapes: dict[str, int],
    dtype: torch.dtype,
    *,
    kwargs: dict[str, Any],
    drift_steps: int,
    atol: float,
    rtol: float,
    device: str = "cuda",
) -> dict[str, Any]:
    """Gate cold and independent recurrent runs, including every inactive state slot."""
    result: dict[str, Any] = {
        "status": "pass",
        "cold_seeds": [0, 1, 17],
        "drift_seed": 101,
        "drift_steps": drift_steps,
        "atol": atol,
        "rtol": rtol,
        "max_output_difference": 0.0,
        "max_state_difference": 0.0,
        "drift_max_output_difference": 0.0,
        "drift_max_state_difference": 0.0,
        "full_state_checked": True,
        "same_inputs": True,
    }
    phase = "cold"
    step = 0
    try:
        if drift_steps < 1:
            raise ValueError("drift_steps must be positive")

        def agree(reference, actual, *, drift: bool) -> None:
            prefix = "drift_" if drift else ""
            for name, expected, observed in zip(
                ("output", "state"), reference, actual, strict=True
            ):
                field = f"{prefix}max_{name}_difference"
                delta = _difference(observed, expected)
                result[field] = None if delta is None else max(result[field] or 0.0, delta)
            for name, expected, observed in zip(
                ("output", "state"), reference, actual, strict=True
            ):
                if not torch.isfinite(observed).all() or not torch.isfinite(expected).all():
                    raise AssertionError(f"{name} contains non-finite values")
                torch.testing.assert_close(observed, expected, atol=atol, rtol=rtol)

        for seed in result["cold_seeds"]:
            inputs = make_inputs(batch, shapes, dtype, seed=seed, device=device)
            agree(_run(stock, inputs, dtype), _run(candidate, inputs, dtype, **kwargs), drift=False)
        # Independent initial state and input seed. Both operations evolve their own
        # state with identical repeated inputs; one-step resets cannot hide accumulation.
        phase = "drift"
        inputs = make_inputs(batch, shapes, dtype, seed=101, device=device)
        reference_state = inputs["state"].clone()
        candidate_state = inputs["state"].clone()
        for step in range(1, drift_steps + 1):  # noqa: B007 - retained for failure metadata
            agree(
                _run(stock, inputs, dtype, state=reference_state),
                _run(candidate, inputs, dtype, state=candidate_state, **kwargs),
                drift=True,
            )
    except Exception as exc:
        result.update(
            status="fail",
            failure_phase=phase,
            failure_step=step,
            error=f"{type(exc).__name__}: {exc}"[:2000],
        )
    return result


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
    out = torch.empty(batch, 1, HV, V, device=state.device, dtype=dtype)
    inner = 20

    def once():
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
        if not torch.isfinite(state).all() or not torch.isfinite(out).all():
            raise ValueError("timing state/output became non-finite")
        return first.elapsed_time(last) * 1000.0 / inner

    for _ in range(_WARMUP):
        once()
    for _ in range(3):
        sample()
    return sample


def _graph_sampler(fn, inputs, dtype, **kwargs):
    """Return a sampler that replays the kernel from a captured CUDA graph.

    This is how vLLM runs decode. Timing individual launches answers a different
    question: it includes the host enqueueing the work, so a kernel shaped to hide launch
    overhead looks better than it will ever be in service, which is exactly the mistake a
    first tuning pass made here.
    """
    HV, V = inputs["shapes"]
    batch = inputs["mixed_qkv"].shape[0]
    state = inputs["state"].clone()
    out = torch.empty(batch, 1, HV, V, device=state.device, dtype=dtype)
    inner = 20

    def once():
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

    # Warm up on a side stream before capture, as capture refuses to record a kernel that
    # still needs compiling.
    side = torch.cuda.Stream()
    side.wait_stream(torch.cuda.current_stream())
    with torch.cuda.stream(side):
        for _ in range(max(1, _WARMUP)):
            once()
    torch.cuda.current_stream().wait_stream(side)
    torch.cuda.synchronize()

    graph = torch.cuda.CUDAGraph()
    with torch.cuda.graph(graph):
        for _ in range(inner):
            once()

    def sample() -> float:
        state.copy_(inputs["state"])
        torch.cuda.synchronize()
        first = torch.cuda.Event(enable_timing=True)
        last = torch.cuda.Event(enable_timing=True)
        first.record()
        graph.replay()
        last.record()
        torch.cuda.synchronize()
        if not torch.isfinite(state).all() or not torch.isfinite(out).all():
            raise ValueError("graph timing state/output became non-finite")
        return first.elapsed_time(last) * 1000.0 / inner

    for _ in range(3):
        sample()
    return sample


def compare_interleaved(
    candidates: dict, inputs, dtype, rounds: int, *, failures: dict[str, str] | None = None
) -> dict[str, float]:
    """Time several implementations by alternating between them.

    Measured one after the other, a GPU that heats up or is shared reports the second as
    slower for reasons that have nothing to do with the code: a first pass on a laptop had
    vLLM slower at eight sequences than at sixteen, which cannot be true. Alternating
    spreads any drift across all of them equally.
    """
    build = _graph_sampler if _USE_GRAPH else _sampler
    errors = {} if failures is None else failures
    samplers = {}
    for name, (fn, kw) in candidates.items():
        try:
            samplers[name] = build(fn, inputs, dtype, **kw)
        except Exception as exc:
            errors[name] = f"{type(exc).__name__}: {exc}"[:2000]
    collected: dict[str, list[float]] = {name: [] for name in samplers}
    for _ in range(rounds):
        for name, sample in samplers.items():
            if name in errors:
                continue
            try:
                value = sample()
                if not math.isfinite(value) or value <= 0:
                    raise ValueError("timing must be positive and finite")
                collected[name].append(value)
            except Exception as exc:
                errors[name] = f"{type(exc).__name__}: {exc}"[:2000]
    return {
        name: statistics.median(values)
        for name, values in collected.items()
        if values and name not in errors
    }


def compare_batch(stock, batch: int, shapes: dict[str, int], dtype, args) -> dict[str, Any]:
    """Validate every requested candidate before allowing any timing or recommendation."""
    candidates = {"fabric": (fused_packed_decode, {})}
    if args.sweep:
        for block_v in (16, 32, 64, 128):
            if block_v > 2 ** (shapes["V"] - 1).bit_length():
                continue
            for warps in (1, 2, 4, 8):
                if warps * 32 <= block_v * 4:
                    candidates[f"{block_v}/{warps}"] = (
                        fused_packed_decode,
                        {"block_v": block_v, "num_warps": warps},
                    )
    checks = {}
    safe = {}
    for label, (candidate, kwargs) in candidates.items():
        checks[label] = check_candidate(
            stock,
            candidate,
            batch,
            shapes,
            dtype,
            kwargs=kwargs,
            drift_steps=args.drift_steps,
            atol=args.atol,
            rtol=args.rtol,
        )
        checks[label]["settings"] = {
            "block_v": kwargs.get("block_v", min(2 ** (shapes["V"] - 1).bit_length(), 32)),
            "num_warps": kwargs.get("num_warps", 1),
            "num_stages": 3,
            "use_qk_l2norm_in_kernel": True,
        }
        if checks[label]["status"] == "pass":
            safe[label] = (candidate, kwargs)
    record: dict[str, Any] = {
        "batch": batch,
        "status": "pass" if len(safe) == len(candidates) else "fail",
        "candidates": checks,
        "max_output_difference": checks["fabric"]["max_output_difference"],
        "max_state_difference": checks["fabric"]["max_state_difference"],
        "timing_seed": 1000 + batch,
        "vllm_us": None,
        "fabric_us": None,
        "speedup": None,
    }
    if safe:
        inputs = make_inputs(batch, shapes, dtype, seed=record["timing_seed"])
        timing_failures: dict[str, str] = {}
        timings = compare_interleaved(
            {"vllm": (stock, {}), **safe},
            inputs,
            dtype,
            max(3, args.reps // 20),
            failures=timing_failures,
        )
        for label, error in timing_failures.items():
            record["status"] = "fail"
            if label == "vllm":
                record["baseline_failure"] = error
            else:
                checks[label].update(status="fail", failure_phase="timing", error=error)
        if "vllm" not in timings:
            return record
        record["vllm_us"] = round(timings["vllm"], 2)
        if "fabric" in timings:
            record["fabric_us"] = round(timings["fabric"], 2)
            record["speedup"] = round(timings["vllm"] / timings["fabric"], 3)
        swept = {label: value for label, value in timings.items() if "/" in label}
        if swept:
            best = min(swept, key=swept.get)
            block_v, warps = best.split("/")
            record.update(
                sweep_vllm_us=round(timings["vllm"], 2),
                best_us=round(swept[best], 2),
                best_block_v=int(block_v),
                best_num_warps=int(warps),
                best_speedup=round(timings["vllm"] / swept[best], 3),
            )
    return record


def _export_artifact(root: pathlib.Path, target, captured, payload) -> pathlib.Path:
    config = {key: value for key, value in payload.items() if key != "results"}
    checks = [
        {"batch": row["batch"], "candidate": label, **check}
        for row in payload["results"]
        for label, check in row["candidates"].items()
    ]
    artifact = build_artifact(
        target=target,
        suite="vllm-packed-decode-comparison",
        status=payload["status"],
        environment=captured,
        config=config,
        correctness=checks,
        measurements=payload["results"],
        drift={"steps": payload["drift_steps"], "checks": checks},
        tile_sweep=checks if payload["sweep"] else None,
        baselines=[
            {
                "name": "stock_vllm_packed_decode",
                "status": "available",
                "version": payload["vllm_version"],
                "module": payload["vllm_module"],
            }
        ],
        claim_scope=_CLAIM_SCOPE,
    )
    verify_artifact(artifact)
    path = artifact_path(root, artifact)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Artifact writer is exclusive: existing evidence must never be overwritten.
    with path.open("x") as handle:
        json.dump(artifact, handle, indent=2, sort_keys=True, allow_nan=False)
        handle.write("\n")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch", type=int, nargs="+", default=[1, 8, 16, 32])
    parser.add_argument("--dtype", default="float16", choices=("float16", "bfloat16", "float32"))
    parser.add_argument("--warmup", type=int, default=30)
    parser.add_argument("--reps", type=int, default=200)
    parser.add_argument("--drift-steps", type=int, default=16)
    parser.add_argument("--atol", type=float, default=0.0)
    parser.add_argument("--rtol", type=float, default=0.0)
    parser.add_argument("--model-config", type=pathlib.Path)
    parser.add_argument("--model-ref", help="model identifier for evidence; never infers shapes")
    parser.add_argument("--model-revision", help="immutable revision identifier for evidence")
    parser.add_argument("--target", help="runtime artifact hardware target, checked before timing")
    parser.add_argument("--artifact-root", type=pathlib.Path)
    parser.add_argument("--sweep", action="store_true", help="check and time other tile shapes")
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--cuda-graph",
        action="store_true",
        help="replay from a captured graph, which is how vLLM runs decode",
    )
    args = parser.parse_args(argv)
    if any(batch <= 0 for batch in args.batch) or args.reps <= 0 or args.warmup < 0:
        parser.error("batch/reps must be positive and warmup must be nonnegative")
    if args.drift_steps < 1:
        parser.error("drift-steps must be positive; correctness drift cannot be disabled")
    if any(not math.isfinite(value) or value < 0 for value in (args.atol, args.rtol)):
        parser.error("atol/rtol must be finite and nonnegative")
    if args.artifact_root is not None and args.target is None:
        parser.error("artifact-root requires --target")
    try:
        shapes, model_metadata = load_model_config(args.model_config)
        target = resolve_target(args.target) if args.target else None
    except (OSError, ValueError) as exc:
        parser.error(str(exc))

    global _USE_GRAPH, _WARMUP
    _USE_GRAPH, _WARMUP = args.cuda_graph, args.warmup
    if not torch.cuda.is_available():
        print("no CUDA device", file=sys.stderr)
        return 2
    captured = environment.capture()
    if target is not None:
        try:
            verify_target(target, captured)
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 2
    ops = resolve()
    if ops is None:
        print("vLLM packed decode op is unavailable; vLLM may not be installed", file=sys.stderr)
        return 2
    if not ops.has_packed_decode:
        print(f"this vLLM ({ops.version}) has no packed decode op", file=sys.stderr)
        return 2
    stock = getattr(ops.packed, "_fabric_original", ops.packed)
    if getattr(stock, "_fabric_substitution", False):
        print("could not resolve the unmodified stock vLLM op", file=sys.stderr)
        return 2

    dtype = getattr(torch, args.dtype)
    results = [compare_batch(stock, batch, shapes, dtype, args) for batch in args.batch]
    payload = {
        "device": captured["gpu"]["name"],
        "compute_capability": captured["gpu"]["compute_capability"],
        "vllm_version": ops.version,
        "vllm_module": ops.module,
        "dtype": args.dtype,
        "shapes": shapes,
        **model_metadata,
        "model_ref": args.model_ref,
        "model_revision": args.model_revision,
        "measured_in_cuda_graph": args.cuda_graph,
        "warmup": args.warmup,
        "reps": args.reps,
        "inner_launches": 20,
        "sweep": args.sweep,
        "drift_steps": args.drift_steps,
        "atol": args.atol,
        "rtol": args.rtol,
        "correctness_gate": "cold-and-independent-recurrent-full-state",
        "claim_scope": _CLAIM_SCOPE,
        "status": "pass" if all(row["status"] == "pass" for row in results) else "fail",
        "results": results,
    }
    if args.artifact_root is not None:
        try:
            payload["artifact_path"] = str(
                _export_artifact(args.artifact_root, target, captured, payload)
            )
        except (OSError, ValueError) as exc:
            print(f"artifact export failed: {exc}", file=sys.stderr)
            return 2
    if args.json:
        print(json.dumps(payload, indent=2, allow_nan=False))
    else:
        print(
            f"{payload['device']} (sm{payload['compute_capability']}), "
            f"vLLM {ops.version}, {args.dtype}"
        )
        print(f"shapes: {shapes} ({model_metadata['shape_source']})")
        for row in results:
            if "baseline_failure" in row:
                print(f"batch={row['batch']} stock timing FAILED: {row['baseline_failure']}")
            if row["fabric_us"] is not None:
                print(
                    f"batch={row['batch']} vllm={row['vllm_us']:.2f}us "
                    f"fabric={row['fabric_us']:.2f}us speedup={row['speedup']:.3f} "
                    f"status={row['status']}"
                )
            for label, check in row["candidates"].items():
                if check["status"] != "pass":
                    print(f"batch={row['batch']} candidate={label} FAILED: {check['error']}")
            if "best_us" in row:
                print(
                    f"best passing tile={row['best_block_v']}/{row['best_num_warps']}w "
                    f"{row['best_us']:.2f}us ({row['best_speedup']:.3f}x)"
                )
        if "artifact_path" in payload:
            print(f"artifact: {payload['artifact_path']}")
    return 0 if payload["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
