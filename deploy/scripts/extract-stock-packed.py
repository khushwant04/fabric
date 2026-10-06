#!/usr/bin/env python3
"""Extract installed stock packed decode without importing the vLLM package.

python /work/fabric-extract-stock-packed.py --output /work/fabric_stock_packed.py \
  --runtime-root /work/runtime --serving-root /work/serving -- \
  --model-config /models/config.json --model-ref Qwen/Qwen3.5-4B \
  --model-revision COMMIT --batch 1 4 16 --dtype float16 --cuda-graph \
  --sweep --target t4-production --artifact-root /evidence --json

Extraction itself uses only Python's standard library. The comparator imports
installed torch/triton afterward. Stock math is kept at FLA_USE_FAST_OPS=0.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.metadata
import importlib.util
import json
import os
import pathlib
import sys
from types import SimpleNamespace

WANTED = (
    "fused_recurrent_gated_delta_rule_packed_decode_kernel",
    "fused_recurrent_gated_delta_rule_packed_decode",
)
CANDIDATES = (
    "vllm/third_party/flash_linear_attention/ops/fused_recurrent.py",
    "vllm/model_executor/layers/fla/ops/fused_recurrent.py",
    "vllm/model_executor/layers/mamba/gdn/fused_recurrent.py",
)


def _read_source(path):
    raw = path.read_bytes()
    return raw.decode("utf-8"), hashlib.sha256(raw).hexdigest()


def _segment(source, node):
    lines = source.splitlines(keepends=True)
    start = min([node.lineno, *(decorator.lineno for decorator in node.decorator_list)])
    return "".join(lines[start - 1 : node.end_lineno])


def extract(source_path, output, *, version, installed):
    if os.environ.get("FLA_USE_FAST_OPS", "0") != "0":
        raise ValueError(
            "FLA_USE_FAST_OPS must be 0; refusing a different stock exp helper"
        )
    source, source_hash = _read_source(source_path)
    tree = ast.parse(source, filename=str(source_path))
    functions = {
        node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)
    }
    if any(name not in functions for name in WANTED):
        raise ValueError(f"packed stock kernel/wrapper missing from {source_path}")
    # This extraction intentionally supports the pinned packed operation's known
    # dependencies. Any future helper or decorator change fails before GPU work.
    globals_used = set()
    for name in WANTED:
        node = functions[name]
        local = {
            arg.arg
            for arg in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)
        }
        local.update(
            item.id
            for item in ast.walk(node)
            if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Store)
        )
        globals_used.update(
            item.id
            for item in ast.walk(node)
            if isinstance(item, ast.Name)
            and isinstance(item.ctx, ast.Load)
            and item.id not in local
        )
        for decorator in node.decorator_list:
            if not (
                isinstance(decorator, ast.Attribute)
                and isinstance(decorator.value, ast.Name)
                and decorator.value.id == "triton"
                and decorator.attr == "jit"
            ):
                raise ValueError(f"unsupported stock decorator on {name}")
    permitted = {
        "torch",
        "triton",
        "tl",
        "exp",
        "float",
        "bool",
        "min",
        "tuple",
        "ValueError",
        *WANTED,
    }
    unexpected = globals_used - permitted
    if unexpected:
        raise ValueError(f"unsupported stock dependencies: {sorted(unexpected)}")
    helper_path = source_path.with_name("op.py")
    helper_source, helper_hash = _read_source(helper_path)
    helper_tree = ast.parse(helper_source)
    default_exp_verified = any(
        isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "exp"
            for target in node.targets
        )
        and isinstance(node.value, ast.Attribute)
        and isinstance(node.value.value, ast.Name)
        and node.value.value.id == "tl"
        and node.value.attr == "exp"
        for branch in helper_tree.body
        if isinstance(branch, ast.If)
        for node in branch.orelse
    )
    if not default_exp_verified:
        raise ValueError("cannot verify stock op.py default exp=tl.exp")
    metadata = {
        "baseline_kind": "installed-source-ast-extraction"
        if installed
        else "explicit-source-ast-extraction",
        "vllm_version": version,
        "stock_source_path": str(source_path.resolve()),
        "stock_source_sha256": source_hash,
        "stock_helper_path": str(helper_path.resolve()),
        "stock_helper_sha256": helper_hash,
        "stock_exp_helper": "triton.language.exp",
        "fla_use_fast_ops": "0",
        "extracted_functions": list(WANTED),
        "claim_scope": (
            "Packed-op synthetic kernel comparison; "
            "no vLLM engine or full-model execution."
        ),
    }
    prefix = "".join(
        line for line in source.splitlines(keepends=True)[:10] if line.startswith("#")
    )
    generated = (
        prefix + "\nimport torch\nimport triton\nimport triton.language as tl\n"
        "exp = tl.exp\nSTOCK_METADATA = "
        + repr(metadata)
        + "\n\n"
        + "\n\n".join(_segment(source, functions[name]) for name in WANTED)
        + "\n"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x") as handle:
        handle.write(generated)
    metadata["extracted_module_sha256"] = hashlib.sha256(
        output.read_bytes()
    ).hexdigest()
    return metadata


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=pathlib.Path,
        default=pathlib.Path("/work/fabric_stock_packed.py"),
    )
    parser.add_argument(
        "--runtime-root", type=pathlib.Path, default=pathlib.Path("/work/runtime")
    )
    parser.add_argument(
        "--serving-root", type=pathlib.Path, default=pathlib.Path("/work/serving")
    )
    parser.add_argument(
        "--source-file",
        type=pathlib.Path,
        help="local extraction check only; version remains unknown",
    )
    parser.add_argument("--extract-only", action="store_true")
    parser.add_argument("benchmark_args", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    try:
        version = None
        source_path = args.source_file
        installed = source_path is None
        if installed:
            distribution = importlib.metadata.distribution("vllm")
            version = distribution.version
            for relative in CANDIDATES:
                candidate = pathlib.Path(distribution.locate_file(relative))
                if candidate.is_file():
                    source_path = candidate
                    break
            if source_path is None:
                raise ValueError(
                    "installed vLLM distribution has no known fused_recurrent.py"
                )
        metadata = extract(
            source_path, args.output, version=version, installed=installed
        )
    except (OSError, ValueError, importlib.metadata.PackageNotFoundError) as exc:
        print(f"stock extraction failed: {exc}", file=sys.stderr)
        return 2
    if args.extract_only:
        print(json.dumps(metadata, indent=2))
        return 0
    if not installed:
        print(
            "explicit source is extraction-only; "
            "benchmark requires installed version metadata",
            file=sys.stderr,
        )
        return 2
    sys.path[:0] = [
        str(args.runtime_root),
        str(args.serving_root),
        str(args.output.parent),
    ]
    spec = importlib.util.spec_from_file_location("fabric_stock_packed", args.output)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    from fabric_serving import compare_packed

    stock = module.fused_recurrent_gated_delta_rule_packed_decode
    compare_packed.resolve = lambda: SimpleNamespace(
        packed=stock,
        has_packed_decode=True,
        version=version,
        module=str(source_path.resolve()),
    )
    def optional_version(name):
        # Environment capture normally imports vLLM just to inspect __version__.
        # Read distribution metadata instead so this path never initializes its package.
        if name == "vllm":
            return version
        try:
            return importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            return None

    compare_packed.environment._optional_version = optional_version
    original_capture = compare_packed.environment.capture

    def capture():
        captured = original_capture()
        captured["vllm_version"] = version
        captured["stock_extraction"] = metadata
        return captured

    compare_packed.environment.capture = capture
    original_export = compare_packed._export_artifact

    def export(root, target, captured, payload):
        payload["stock_extraction"] = metadata
        return original_export(root, target, captured, payload)

    compare_packed._export_artifact = export
    original_dumps = compare_packed.json.dumps

    def dumps(payload, *positional, **keywords):
        if (
            isinstance(payload, dict)
            and "results" in payload
            and "vllm_module" in payload
        ):
            payload = {**payload, "stock_extraction": metadata}
        return original_dumps(payload, *positional, **keywords)

    compare_packed.json.dumps = dumps
    benchmark_args = args.benchmark_args
    if benchmark_args[:1] == ["--"]:
        benchmark_args = benchmark_args[1:]
    return compare_packed.main(benchmark_args)


if __name__ == "__main__":
    raise SystemExit(main())
