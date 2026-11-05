"""Command line: ``python -m benchmarks.routing`` from ``data-plane/``.

    python -m benchmarks.routing \\
        --strategies round_robin,least_in_flight,session_affinity,prefix_aware \\
        --workloads multi_turn,shared_prefix,mixed --rates 0.5,1,2,4 --repeats 5

Every combination of strategy, workload, rate and seed is one run on a fresh fleet, so no
run inherits another's warm caches. Results go to ``--out`` (default ``results/<time>``).
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import datetime as dt
import pathlib
import sys

from benchmarks.routing.harness import RunConfig, calibrate, run
from benchmarks.routing.report import Slo, write_all
from benchmarks.routing.simhost import HostModel
from benchmarks.routing.workload import WORKLOADS, WorkloadConfig
from fabric_data_plane.pool import STRATEGIES


def _csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m benchmarks.routing", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--strategies", type=_csv,
                        default=["round_robin", "least_in_flight", "session_affinity",
                                 "prefix_aware"])
    parser.add_argument("--workloads", type=_csv, default=list(WORKLOADS))
    parser.add_argument("--rates", type=lambda v: [float(x) for x in _csv(v)],
                        default=[0.5, 1.0, 2.0])
    parser.add_argument("--duration", type=float, default=60.0,
                        help="simulated seconds over which sessions start")
    parser.add_argument("--repeats", type=int, default=1, help="seeds per point")
    parser.add_argument("--seed", type=int, default=0, help="first seed")
    parser.add_argument("--backends", type=int, default=4)
    parser.add_argument("--time-scale", type=float, default=0.25,
                        help="wall seconds per simulated second")
    parser.add_argument("--system-prompts", type=int, default=WorkloadConfig.system_prompts,
                        help="distinct system prompts; more than fit in one host's cache "
                             "makes where a request lands matter for shared prefixes")
    parser.add_argument("--no-user-field", action="store_true",
                        help="omit the OpenAI user field, which session affinity keys on")
    parser.add_argument("--slo-ttft-ms", type=float, default=500.0)
    parser.add_argument("--slo-tpot-ms", type=float, default=60.0)
    parser.add_argument("--cache-blocks", type=int, default=HostModel.cache_blocks)
    parser.add_argument("--out", type=pathlib.Path, default=None)
    arguments = parser.parse_args(argv)
    unknown = set(arguments.strategies) - set(STRATEGIES)
    if unknown:
        parser.error(f"unknown strategies {sorted(unknown)}; the gateway has {list(STRATEGIES)}")
    unknown = set(arguments.workloads) - set(WORKLOADS)
    if unknown:
        parser.error(f"unknown workloads {sorted(unknown)}; choose from {list(WORKLOADS)}")
    return arguments


async def _main(arguments: argparse.Namespace, argv: list[str]) -> pathlib.Path:
    out = arguments.out or pathlib.Path("results") / dt.datetime.now(tz=dt.UTC).strftime(
        "routing-%Y%m%dT%H%M%SZ"
    )
    host = dataclasses.replace(HostModel(), cache_blocks=arguments.cache_blocks)
    print(f"calibrating gateway overhead at time scale {arguments.time_scale} ...", flush=True)
    calibration = await calibrate(time_scale=arguments.time_scale)
    runs = []
    points = [
        (workload, rate, seed, strategy)
        for workload in arguments.workloads
        for rate in arguments.rates
        for seed in range(arguments.seed, arguments.seed + arguments.repeats)
        for strategy in arguments.strategies
    ]
    for index, (workload, rate, seed, strategy) in enumerate(points, start=1):
        config = RunConfig(
            strategy=strategy,
            workload=WorkloadConfig(
                kind=workload, rate=rate, duration_seconds=arguments.duration, seed=seed,
                system_prompts=arguments.system_prompts,
                set_user_field=not arguments.no_user_field,
            ),
            backends=arguments.backends,
            host=host,
            time_scale=arguments.time_scale,
        )
        result = await run(config)
        ok = sum(1 for request in result.requests if request.ok)
        print(f"[{index}/{len(points)}] {workload} rate={rate:g} seed={seed} {strategy}: "
              f"{ok}/{len(result.requests)} completed in {result.wall_seconds:.1f}s", flush=True)
        runs.append(result)
    slo = Slo(ttft_ms=arguments.slo_ttft_ms, tpot_ms=arguments.slo_tpot_ms)
    artifact = write_all(out, runs, slo=slo, calibration=calibration, argv=argv)
    if not artifact["figures"]:
        print("matplotlib is not installed; figures were skipped", flush=True)
    return out


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    out = asyncio.run(_main(_parse(argv), argv))
    print(f"wrote {out}/results.json, requests.csv, summary.md")
    print((out / "summary.md").read_text())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
