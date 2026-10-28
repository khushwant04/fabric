"""The routing benchmark harness: workloads, the simulated host, metrics, and one live run."""

from __future__ import annotations

import pytest

from benchmarks.routing.harness import RequestResult, RunConfig, RunResult, run
from benchmarks.routing.report import Slo, percentile, summarize, tpot
from benchmarks.routing.simhost import HostModel, SimulatedHost, _block_hashes
from benchmarks.routing.workload import WorkloadConfig, count_turns, generate

# --- workloads -----------------------------------------------------------------------------


def _turns(first):
    turn = first
    while turn is not None:
        yield turn
        turn = turn.next_turn


def test_a_workload_is_a_pure_function_of_its_configuration() -> None:
    config = WorkloadConfig(kind="mixed", rate=3.0, duration_seconds=20.0, seed=4)
    assert generate(config) == generate(config)
    assert generate(config) != generate(WorkloadConfig(
        kind="mixed", rate=3.0, duration_seconds=20.0, seed=5
    ))


def test_each_turn_resends_the_whole_conversation_before_its_new_message() -> None:
    sessions = generate(WorkloadConfig(kind="multi_turn", rate=1.0, duration_seconds=10.0))
    turns = list(_turns(sessions[0]))
    assert len(turns) == WorkloadConfig().turns_per_session
    for earlier, later in zip(turns, turns[1:], strict=False):
        # The earlier prompt and its reply are a strict prefix of the later prompt, which is
        # what a prefix cache can reuse.
        assert later.messages[: len(earlier.messages)] == earlier.messages
        assert later.messages[len(earlier.messages)].role == "assistant"
        assert len(later.messages[len(earlier.messages)].tokens) == earlier.output_tokens


def test_shared_prefix_requests_are_single_turn_and_share_system_prompts() -> None:
    sessions = generate(WorkloadConfig(
        kind="shared_prefix", rate=5.0, duration_seconds=10.0, system_prompts=2
    ))
    assert count_turns(sessions) == len(sessions)
    systems = {session.messages[0] for session in sessions}
    assert len(systems) == 2


def test_an_unknown_workload_is_refused() -> None:
    with pytest.raises(ValueError):
        WorkloadConfig(kind="nonsense")


# --- simulated host ------------------------------------------------------------------------


def test_block_hashes_identify_a_block_by_everything_before_it() -> None:
    first = _block_hashes(list(range(48)), 16)
    same_prefix = _block_hashes(list(range(32)) + [999] * 16, 16)
    assert first[:2] == same_prefix[:2]
    assert first[2] != same_prefix[2]
    # The same block content after a different prefix is a different block.
    assert _block_hashes([7] * 16 + list(range(16)), 16)[1] != _block_hashes(list(range(16)), 16)[0]


async def _drain(host: SimulatedHost, payload: dict) -> list[bytes]:
    return [chunk async for chunk in host.stream(payload)]


def _payload(tokens: list[int], output: int = 2) -> dict:
    return {
        "messages": [{"role": "user", "content": " ".join(f"t{t}" for t in tokens)}],
        "stream": True, "max_tokens": output, "stream_options": {"include_usage": True},
    }


async def test_a_repeated_prompt_prefills_only_what_the_cache_lacks() -> None:
    slept: list[float] = []

    async def record(seconds: float) -> None:
        slept.append(seconds)

    model = HostModel(prefill_ms_per_token=1.0, prefill_overhead_ms=0.0, decode_ms_base=0.0,
                      decode_ms_per_sequence=0.0)
    host = SimulatedHost("b0", model, record)
    await _drain(host, _payload(list(range(64))))
    first_prefill = slept[0]
    slept.clear()
    await _drain(host, _payload(list(range(64)) + list(range(1000, 1010))))
    assert first_prefill == pytest.approx(0.064)
    assert slept[0] == pytest.approx(0.010)  # only the ten new tokens
    assert host.stats.cached_tokens == 64


async def test_least_recently_used_blocks_are_evicted() -> None:
    async def instant(_seconds: float) -> None:
        return None

    host = SimulatedHost("b0", HostModel(cache_blocks=4), instant)
    await _drain(host, _payload(list(range(64))))  # four blocks: the cache is full
    await _drain(host, _payload(list(range(500, 564))))  # four more evict them
    before = host.stats.cached_tokens
    await _drain(host, _payload(list(range(64))))
    assert host.stats.cached_tokens == before  # the first prompt is no longer cached


async def test_the_host_reports_cached_tokens_the_way_engines_do() -> None:
    async def instant(_seconds: float) -> None:
        return None

    host = SimulatedHost("b7", HostModel(), instant)
    await _drain(host, _payload(list(range(32))))
    frames = await _drain(host, _payload(list(range(32))))
    usage_frame = next(frame for frame in frames if b'"usage"' in frame)
    assert b'"cached_tokens":32' in usage_frame
    assert b'"system_fingerprint":"b7"' in frames[0]
    assert frames[-1] == b"data: [DONE]\n\n"


# --- metrics -------------------------------------------------------------------------------


def test_percentiles_interpolate_between_ranks() -> None:
    assert percentile([1.0, 2.0, 3.0, 4.0], 0.5) == pytest.approx(2.5)
    assert percentile([10.0], 0.95) == 10.0
    assert percentile([], 0.5) is None


def _result(ttft: float, latency: float, out: int, backend: str, ok: bool = True) -> RequestResult:
    return RequestResult(
        session_id="s", turn=0, started=0.0, status=200 if ok else 503,
        ttft=ttft if ok else None, latency=latency if ok else None,
        prompt_tokens=100, output_tokens=out, cached_tokens=50, backend=backend,
    )


def test_the_summary_counts_goodput_imbalance_and_cache_hits() -> None:
    requests = [
        _result(0.1, 1.1, 11, "a"),   # TPOT 100 ms: misses a 60 ms SLO
        _result(0.2, 0.7, 11, "a"),   # TPOT 50 ms, TTFT 200 ms: good
        _result(0.9, 1.4, 11, "b"),   # TTFT 900 ms: misses
        _result(0.0, 0.0, 0, "b", ok=False),
    ]
    assert tpot(requests[0]) == pytest.approx(0.1)
    summary = summarize(
        RunResult(
            config=RunConfig(strategy="round_robin", workload=WorkloadConfig()),
            requests=requests,
            host_stats={"a": {}, "b": {}, "c": {}},
            wall_seconds=1.0,
            span_seconds=2.0,
        ),
        Slo(ttft_ms=500, tpot_ms=60),
    )
    assert summary["completed"] == 3 and summary["failed"] == 1
    assert summary["goodput_fraction"] == pytest.approx(0.25)
    assert summary["cache_hit_rate"] == pytest.approx(0.5)
    # Two requests on a, one on b, none on c: the busiest carries twice the mean.
    assert summary["load_imbalance"] == pytest.approx(2.0)


# --- through the real gateway --------------------------------------------------------------


async def test_a_short_run_through_the_gateway_completes_and_reuses_the_cache() -> None:
    config = RunConfig(
        strategy="session_affinity",
        workload=WorkloadConfig(kind="multi_turn", rate=2.0, duration_seconds=3.0, seed=3,
                                turns_per_session=3, think_seconds=0.5, mean_output_tokens=16),
        backends=2,
        time_scale=0.05,
    )
    result = await run(config)
    assert result.requests
    assert all(request.ok for request in result.requests), {r.error for r in result.requests}
    assert {request.backend for request in result.requests} <= {"backend-0", "backend-1"}
    # Follow-up turns land on the backend holding their history, so some prompt is reused.
    assert sum(request.cached_tokens for request in result.requests) > 0
    assert len(result.requests) == count_turns(generate(config.workload))
