"""Sweep packed-decode launch shapes on the GPU present and write a launch table.

For every batch, every candidate ``block_v`` x ``num_warps`` x ``num_stages`` is first
checked against the independent eager reference (output and every recurrent-state element,
three cold seeds and a multi-step recurrent drift run). Only candidates that pass are
timed, interleaved so thermal drift spreads across all of them, by default replayed from a
captured CUDA graph because that is how vLLM runs decode.

Batches are grouped into the buckets :mod:`kernels.launch_config` uses. Within a bucket the
selected shape is the passing candidate with the lowest mean latency relative to the best
candidate at each of the bucket's batches, so a bucket is not decided by its largest batch.

Two files come out:

* a versioned artifact under ``<artifact-root>/<target>/`` via :mod:`harness.artifact`,
  carrying every candidate's correctness and timing, and
* a tuning JSON (``fabric-kernel-tuning/v1``) next to it under ``tuning/``, which
  ``FABRIC_KERNEL_TUNING_FILE`` consumes without a code change.

The declared ``--target`` must match the GPU present, checked before anything runs, and
entries are keyed by the compute capability actually detected, so a result cannot be filed
under another GPU's name.

This measures one kernel on synthetic inputs. It chooses tiles; it is not a full-model or
serving claim.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import pathlib
import statistics
import sys
from collections.abc import Callable
from typing import Any

import torch

RUNTIME_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNTIME_ROOT))

from harness import environment  # noqa: E402
from harness.artifact import (  # noqa: E402
    TargetMismatchError,
    artifact_path,
    build_artifact,
    resolve_target,
    verify_artifact,
    verify_target,
)
from kernels import launch_config  # noqa: E402

SUITE = "packed-decode-launch-tuning"
CLAIM_SCOPE = (
    "Synthetic single-kernel launch-shape sweep of the Fabric packed gated-delta decode "
    "kernel against its eager reference, used to select tiles. Not a full-model, "
    "generation-quality, or end-to-end serving performance claim."
)
DEFAULT_BATCHES = (1, 2, 4, 8, 16, 32, 64)
DEFAULT_BLOCK_V = (16, 32, 64, 128)
DEFAULT_WARPS = (1, 2, 4, 8)
COLD_SEEDS = (0, 1, 17)
DRIFT_SEED = 101
INNER_LAUNCHES = 20


def default_stages(capability: tuple[int, int]) -> tuple[int, ...]:
    """Stages worth sweeping on this architecture.

    Triton's software pipelining is driven by asynchronous copies that SM75 does not have,
    so on Turing only 1 and 2 are swept, plus 3 because that is what the committed T4
    evidence was recorded with.
    """
    return (1, 2, 3) if tuple(capability) < (8, 0) else (1, 2, 3, 4)


def candidate_grid(
    value_dim: int,
    block_vs: tuple[int, ...],
    warps: tuple[int, ...],
    stages: tuple[int, ...],
) -> list[dict[str, int]]:
    """Every distinct launch shape worth checking.

    Tiles wider than the padded value dimension collapse onto it, so they are dropped. Warp
    counts are capped at ``block_v / 8`` (``warps * 32 <= block_v * 4``), the rule the
    existing vLLM comparator uses, so no warp is left holding a sliver of the tile.
    """
    widest = 1 << (value_dim - 1).bit_length()
    grid = []
    for block_v in sorted(set(block_vs)):
        if block_v > widest:
            continue
        for warp in sorted(set(warps)):
            if warp * 32 > block_v * 4:
                continue
            for stage in sorted(set(stages)):
                grid.append({"block_v": block_v, "num_warps": warp, "num_stages": stage})
    return grid


def label_of(config: dict[str, int]) -> str:
    return f"{config['block_v']}/{config['num_warps']}/{config['num_stages']}"


# --- inputs, correctness -----------------------------------------------------------------


def make_inputs(
    batch: int, shapes: dict[str, int], dtype: torch.dtype, seed: int, *, device: str = "cuda"
) -> dict[str, Any]:
    generator = torch.Generator(device=device).manual_seed(seed)
    H, HV, K, V = shapes["H"], shapes["HV"], shapes["K"], shapes["V"]
    mixed = torch.randn(batch, 2 * H * K + HV * V, device=device, dtype=dtype, generator=generator)
    a = torch.randn(batch, HV, device=device, dtype=dtype, generator=generator)
    b = torch.randn(batch, HV, device=device, dtype=dtype, generator=generator)
    A_log = torch.randn(HV, device=device, dtype=torch.float32, generator=generator)
    dt_bias = torch.randn(HV, device=device, dtype=torch.float32, generator=generator)
    slots = batch + 3  # slot 0 means "no state"; spare slots must stay untouched
    state = torch.randn(slots, HV, V, K, device=device, generator=generator) * 0.1
    indices = torch.randperm(slots - 1, device=device, generator=generator)[:batch] + 1
    return {
        "mixed_qkv": mixed,
        "a": a,
        "b": b,
        "A_log": A_log,
        "dt_bias": dt_bias,
        "scale": float(K) ** -0.5,
        "state": state,
        "indices": indices.to(torch.int32).contiguous(),
        "HV": HV,
        "V": V,
        "dtype": dtype,
    }


def run_once(fn: Callable[..., Any], inputs: dict[str, Any], state: torch.Tensor, **kwargs):
    batch = inputs["mixed_qkv"].shape[0]
    out = torch.empty(
        batch, 1, inputs["HV"], inputs["V"], device=state.device, dtype=inputs["dtype"]
    )
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


def check_candidate(
    kernel: Callable[..., Any],
    reference: Callable[..., Any],
    config: dict[str, int],
    default: dict[str, int],
    batch: int,
    shapes: dict[str, int],
    dtype: torch.dtype,
    *,
    drift_steps: int,
    atol: float,
    rtol: float,
    device: str = "cuda",
) -> dict[str, Any]:
    """Gate one candidate at one batch; never raises."""
    result: dict[str, Any] = {
        "status": "pass",
        "cold_seeds": list(COLD_SEEDS),
        "drift_seed": DRIFT_SEED,
        "drift_steps": drift_steps,
        "atol": atol,
        "rtol": rtol,
        "max_output_difference": 0.0,
        "max_state_difference": 0.0,
        "bitwise_equal_to_default": True,
        "full_state_checked": True,
    }
    phase = "cold"
    try:

        def agree(expected, actual) -> None:
            for name, want, got in zip(("output", "state"), expected, actual, strict=True):
                if not torch.isfinite(got).all():
                    raise AssertionError(f"{name} contains non-finite values")
                delta = (got.float() - want.float()).abs().max().item()
                field = f"max_{name}_difference"
                result[field] = max(result[field], float(delta))
                torch.testing.assert_close(got, want, atol=atol, rtol=rtol)

        for seed in COLD_SEEDS:
            inputs = make_inputs(batch, shapes, dtype, seed, device=device)
            expected = run_once(reference, inputs, inputs["state"].clone())
            actual = run_once(kernel, inputs, inputs["state"].clone(), **config)
            agree(expected, actual)
            baseline = run_once(kernel, inputs, inputs["state"].clone(), **default)
            if not all(torch.equal(x, y) for x, y in zip(actual, baseline, strict=True)):
                result["bitwise_equal_to_default"] = False

        phase = "drift"
        inputs = make_inputs(batch, shapes, dtype, DRIFT_SEED, device=device)
        reference_state = inputs["state"].clone()
        candidate_state = inputs["state"].clone()
        for step in range(1, drift_steps + 1):
            result["failure_step"] = step
            agree(
                run_once(reference, inputs, reference_state),
                run_once(kernel, inputs, candidate_state, **config),
            )
        result.pop("failure_step", None)
    except Exception as exc:  # noqa: BLE001 - a bad candidate is a recorded result
        result.update(
            status="fail", failure_phase=phase, error=f"{type(exc).__name__}: {exc}"[:2000]
        )
    return result


# --- timing ------------------------------------------------------------------------------


def _sampler(fn, inputs, *, use_graph: bool, warmup: int, **kwargs) -> Callable[[], float]:
    """Return a callable giving one sample in microseconds per launch."""
    state = inputs["state"].clone()
    batch = inputs["mixed_qkv"].shape[0]
    out = torch.empty(
        batch, 1, inputs["HV"], inputs["V"], device=state.device, dtype=inputs["dtype"]
    )
    args = (
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
    )

    def once() -> None:
        fn(*args, **kwargs)

    side = torch.cuda.Stream()
    side.wait_stream(torch.cuda.current_stream())
    with torch.cuda.stream(side):
        for _ in range(max(1, warmup)):
            once()
    torch.cuda.current_stream().wait_stream(side)
    torch.cuda.synchronize()

    if use_graph:
        graph = torch.cuda.CUDAGraph()
        with torch.cuda.graph(graph):
            for _ in range(INNER_LAUNCHES):
                once()

        def body() -> None:
            graph.replay()

    else:

        def body() -> None:
            for _ in range(INNER_LAUNCHES):
                once()

    def sample() -> float:
        state.copy_(inputs["state"])  # keep the state out of denormals across samples
        torch.cuda.synchronize()
        first = torch.cuda.Event(enable_timing=True)
        last = torch.cuda.Event(enable_timing=True)
        first.record()
        body()
        last.record()
        torch.cuda.synchronize()
        if not torch.isfinite(state).all() or not torch.isfinite(out).all():
            raise ValueError("timing state/output became non-finite")
        return first.elapsed_time(last) * 1000.0 / INNER_LAUNCHES

    for _ in range(3):
        sample()
    return sample


def time_interleaved(
    fn, candidates: dict[str, dict[str, int]], inputs, *, rounds: int, use_graph: bool, warmup: int
) -> tuple[dict[str, float], dict[str, str]]:
    failures: dict[str, str] = {}
    samplers = {}
    for label, config in candidates.items():
        try:
            samplers[label] = _sampler(fn, inputs, use_graph=use_graph, warmup=warmup, **config)
        except Exception as exc:  # noqa: BLE001
            failures[label] = f"{type(exc).__name__}: {exc}"[:2000]
    collected: dict[str, list[float]] = {label: [] for label in samplers}
    for _ in range(rounds):
        for label, sample in samplers.items():
            if label in failures:
                continue
            try:
                value = sample()
                if not math.isfinite(value) or value <= 0:
                    raise ValueError("timing must be positive and finite")
                collected[label].append(value)
            except Exception as exc:  # noqa: BLE001
                failures[label] = f"{type(exc).__name__}: {exc}"[:2000]
    medians = {
        label: statistics.median(values)
        for label, values in collected.items()
        if values and label not in failures
    }
    return medians, failures


# --- selection and output ----------------------------------------------------------------


def select_per_bucket(
    timings: dict[int, dict[str, float]], configs: dict[str, dict[str, int]]
) -> dict[str, dict[str, Any]]:
    """Choose one shape per bucket from per-batch median timings of passing candidates.

    A candidate is eligible for a bucket only if it was timed at every batch swept in that
    bucket. The score is its mean latency relative to the fastest candidate at each batch.
    Ties go to the smaller tile, then fewer warps, then fewer stages.
    """
    by_bucket: dict[str, list[int]] = {}
    for batch in sorted(timings):
        by_bucket.setdefault(launch_config.batch_bucket(batch), []).append(batch)
    selections: dict[str, dict[str, Any]] = {}
    for bucket, batches in by_bucket.items():
        eligible = set.intersection(*(set(timings[batch]) for batch in batches))
        if not eligible:
            continue
        best_at = {batch: min(timings[batch].values()) for batch in batches}

        def score(label: str, batches=batches, best_at=best_at) -> float:
            return statistics.fmean(timings[b][label] / best_at[b] for b in batches)

        def key(label: str) -> tuple[float, int, int, int]:
            config = configs[label]
            return (
                round(score(label), 9),
                config["block_v"],
                config["num_warps"],
                config["num_stages"],
            )

        winner = min(eligible, key=key)
        selections[bucket] = {
            "label": winner,
            **configs[winner],
            "batches": batches,
            "relative_score": round(score(winner), 4),
            "us": {str(b): round(timings[b][winner], 3) for b in batches},
        }
    return selections


def build_tuning_document(
    *,
    selections: dict[str, dict[str, Any]],
    gpu_name: str,
    capability: str,
    target: str,
    context: dict[str, Any],
    artifact: dict[str, Any] | None,
    created_at: str,
) -> dict[str, Any]:
    entries = []
    for bucket in launch_config.BUCKET_LABELS:
        chosen = selections.get(bucket)
        if chosen is None:
            continue
        entries.append(
            {
                "kernel": launch_config.KERNEL_PACKED,
                "compute_capability": capability,
                "batch_bucket": bucket,
                "block_v": chosen["block_v"],
                "num_warps": chosen["num_warps"],
                "num_stages": chosen["num_stages"],
                "measured": True,
                "note": (
                    f"Swept on {gpu_name} (sm{capability}) at batches {chosen['batches']}, "
                    f"{context.get('dtype')}, shapes {context.get('shapes')}, "
                    f"cuda_graph={context.get('cuda_graph')}; median us {chosen['us']}"
                ),
            }
        )
    document = {
        "schema": launch_config.TUNING_SCHEMA,
        "generated_by": "harness.tune",
        "created_at": created_at,
        "target": target,
        "gpu": {"name": gpu_name, "compute_capability": capability},
        "context": context,
        "artifact": artifact,
        "entries": entries,
    }
    # The consumer's own validation, so a file this writes is always one it accepts.
    launch_config.parse_tuning(document, origin="generated tuning document")
    return document


def write_exclusive(path: pathlib.Path, payload: dict[str, Any]) -> pathlib.Path:
    """Write JSON without ever overwriting existing evidence."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True, allow_nan=False)
        handle.write("\n")
    return path


def tuning_path_for(artifact_file: pathlib.Path) -> pathlib.Path:
    return artifact_file.parent / "tuning" / f"{artifact_file.stem}.tuning.json"


# --- command line ------------------------------------------------------------------------


def _positive_ints(
    values: list[int], name: str, parser: argparse.ArgumentParser
) -> tuple[int, ...]:
    if not values or any(value <= 0 for value in values):
        parser.error(f"{name} must be positive integers")
    return tuple(values)


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--target", required=True, help="artifact hardware target; must match the GPU"
    )
    parser.add_argument("--artifact-root", type=pathlib.Path, default=RUNTIME_ROOT / "artifacts")
    parser.add_argument("--tuning-output", type=pathlib.Path, help="default: next to the artifact")
    parser.add_argument("--batch", type=int, nargs="+", default=list(DEFAULT_BATCHES))
    parser.add_argument("--block-v", type=int, nargs="+", default=list(DEFAULT_BLOCK_V))
    parser.add_argument("--num-warps", type=int, nargs="+", default=list(DEFAULT_WARPS))
    parser.add_argument("--num-stages", type=int, nargs="+", help="default depends on the GPU")
    parser.add_argument("--heads", type=int, default=16, help="query/key heads (H)")
    parser.add_argument("--value-heads", type=int, default=16, help="value heads (HV)")
    parser.add_argument("--key-dim", type=int, default=128)
    parser.add_argument("--value-dim", type=int, default=128)
    parser.add_argument("--dtype", default="float16", choices=("float16", "bfloat16", "float32"))
    parser.add_argument("--drift-steps", type=int, default=16)
    parser.add_argument("--atol", type=float, default=2e-3)
    parser.add_argument("--rtol", type=float, default=2e-3)
    parser.add_argument(
        "--require-bitwise-default",
        action="store_true",
        help="exclude candidates whose output/state differ in any bit from vLLM's tile (32/1/3)",
    )
    parser.add_argument("--warmup", type=int, default=30)
    parser.add_argument("--rounds", type=int, default=10)
    parser.add_argument("--no-cuda-graph", action="store_true", help="time eager launches")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    args.batch = _positive_ints(args.batch, "--batch", parser)
    args.block_v = _positive_ints(args.block_v, "--block-v", parser)
    args.num_warps = _positive_ints(args.num_warps, "--num-warps", parser)
    if args.num_stages is not None:
        args.num_stages = _positive_ints(args.num_stages, "--num-stages", parser)
    if args.drift_steps < 1:
        parser.error("--drift-steps must be positive; drift checking cannot be disabled")
    if args.rounds < 1 or args.warmup < 0:
        parser.error("--rounds must be positive and --warmup nonnegative")
    if any(not math.isfinite(v) or v < 0 for v in (args.atol, args.rtol)):
        parser.error("--atol/--rtol must be finite and nonnegative")
    if args.value_heads % args.heads:
        parser.error("--value-heads must be divisible by --heads")
    try:
        args.target = resolve_target(args.target)
    except ValueError as exc:
        parser.error(str(exc))
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not torch.cuda.is_available():
        print("no CUDA device: the sweep must run on the GPU it is labelled for", file=sys.stderr)
        return 2
    captured = environment.capture()
    try:
        # Before any work: a result labelled for another GPU must never be produced.
        verify_target(args.target, captured)
    except TargetMismatchError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    gpu_name = captured["gpu"]["name"]
    capability_text = captured["gpu"]["compute_capability"]
    capability = tuple(int(part) for part in capability_text.split("."))
    dtype = getattr(torch, args.dtype)
    refusal = launch_config.bf16_unsupported_reason(dtype, capability)
    if refusal:
        print(refusal, file=sys.stderr)
        return 2

    from kernels.gated_delta_packed_decode import (
        fused_packed_decode,
        torch_packed_decode_reference,
    )

    shapes = {"H": args.heads, "HV": args.value_heads, "K": args.key_dim, "V": args.value_dim}
    stages = args.num_stages or default_stages(capability)
    grid = candidate_grid(args.value_dim, args.block_v, args.num_warps, stages)
    if not grid:
        print("no candidate launch shapes to sweep", file=sys.stderr)
        return 2
    configs = {label_of(config): config for config in grid}
    default = {
        "block_v": min(1 << (args.value_dim - 1).bit_length(), 32),
        "num_warps": 1,
        "num_stages": 3,
    }

    checks: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    timings: dict[int, dict[str, float]] = {}
    failed = 0
    for batch in args.batch:
        passing: dict[str, dict[str, int]] = {}
        for label, config in configs.items():
            check = check_candidate(
                fused_packed_decode,
                torch_packed_decode_reference,
                config,
                default,
                batch,
                shapes,
                dtype,
                drift_steps=args.drift_steps,
                atol=args.atol,
                rtol=args.rtol,
            )
            if (
                check["status"] == "pass"
                and args.require_bitwise_default
                and not check["bitwise_equal_to_default"]
            ):
                check["status"] = "excluded"
                check["error"] = "not bitwise identical to vLLM's tile (--require-bitwise-default)"
            if check["status"] == "fail":
                failed += 1
            if check["status"] == "pass":
                passing[label] = config
            checks.append({"batch": batch, "candidate": label, "settings": config, **check})
        inputs = make_inputs(batch, shapes, dtype, 1000 + batch)
        medians, failures = time_interleaved(
            fused_packed_decode,
            passing,
            inputs,
            rounds=args.rounds,
            use_graph=not args.no_cuda_graph,
            warmup=args.warmup,
        )
        failed += len(failures)
        timings[batch] = medians
        for label in configs:
            rows.append(
                {
                    "batch": batch,
                    "candidate": label,
                    **configs[label],
                    "us": None if label not in medians else round(medians[label], 3),
                    "timing_error": failures.get(label),
                }
            )
        print(
            f"batch {batch}: {len(passing)}/{len(configs)} passed, {len(medians)} timed",
            file=sys.stderr,
        )

    selections = select_per_bucket(timings, configs)
    status = "pass" if failed == 0 and selections else "fail"
    context = {
        "dtype": args.dtype,
        "shapes": shapes,
        "batches": list(args.batch),
        "cuda_graph": not args.no_cuda_graph,
        "atol": args.atol,
        "rtol": args.rtol,
        "drift_steps": args.drift_steps,
        "require_bitwise_default": args.require_bitwise_default,
        "stages_swept": list(stages),
    }
    config_record = {
        **context,
        "device": gpu_name,
        "compute_capability": capability_text,
        "candidates": list(configs),
        "default_tile": default,
        "rounds": args.rounds,
        "warmup": args.warmup,
        "inner_launches": INNER_LAUNCHES,
        "selections": selections,
        "claim_scope": CLAIM_SCOPE,
    }
    artifact = build_artifact(
        target=args.target,
        suite=SUITE,
        status=status,
        environment=captured,
        config=config_record,
        correctness=checks,
        measurements=rows,
        drift={"steps": args.drift_steps, "seed": DRIFT_SEED},
        tile_sweep=rows,
        claim_scope=CLAIM_SCOPE,
    )
    verify_artifact(artifact)
    try:
        artifact_file = write_exclusive(artifact_path(args.artifact_root, artifact), artifact)
    except OSError as exc:
        print(f"artifact export failed: {exc}", file=sys.stderr)
        return 2

    payload: dict[str, Any] = {
        "status": status,
        "artifact_path": str(artifact_file),
        "selections": selections,
        "failed_candidates": failed,
    }
    if selections:
        document = build_tuning_document(
            selections=selections,
            gpu_name=gpu_name,
            capability=capability_text,
            target=args.target.name,
            context=context,
            artifact={"path": str(artifact_file), "content_hash": artifact["content_hash"]},
            created_at=dt.datetime.now(tz=dt.UTC).isoformat().replace("+00:00", "Z"),
        )
        try:
            tuning_file = write_exclusive(
                args.tuning_output or tuning_path_for(artifact_file), document
            )
        except OSError as exc:
            print(f"tuning file export failed: {exc}", file=sys.stderr)
            return 2
        payload["tuning_path"] = str(tuning_file)

    if args.json:
        print(json.dumps(payload, indent=2, allow_nan=False))
    else:
        print(f"{gpu_name} (sm{capability_text}) {args.dtype} shapes={shapes}")
        for bucket, chosen in selections.items():
            print(f"bucket {bucket:>5}: {chosen['label']}  us={chosen['us']}")
        if failed:
            print(f"{failed} candidate checks or timings failed; see the artifact")
        print(f"artifact: {artifact_file}")
        if "tuning_path" in payload:
            print(f"tuning file: {payload['tuning_path']}")
    return 0 if status == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
