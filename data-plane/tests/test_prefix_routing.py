"""Prefix-aware routing (ADR 0019): prompt-prefix hashes and the bounded-load strategy."""

from __future__ import annotations

import dataclasses

import httpx

from fabric_data_plane.pool import (
    STRATEGIES,
    Backend,
    BackendHealth,
    BackendPool,
    PrefixAwareStrategy,
)
from fabric_data_plane.prefix import BLOCK_CHARS, prefix_blocks, prompt_text
from fabric_data_plane.registry import DeploymentRegistry
from tests.conftest import ACCOUNT_A, SigningKey
from tests.test_router import FleetUpstream, _make_plane, _registry

SYSTEM = "You are a careful assistant. " * 40  # several blocks long


def _chat(*turns: str, system: str = SYSTEM) -> dict:
    messages = [{"role": "system", "content": system}]
    for index, text in enumerate(turns):
        messages.append({"role": "user" if index % 2 == 0 else "assistant", "content": text})
    return {"model": "launch-model", "messages": messages}


# --- prompt-prefix hashes ------------------------------------------------------------------


def test_a_follow_up_turn_keeps_every_block_hash_of_the_turn_before_it() -> None:
    first = prefix_blocks(_chat("question " * 60))
    later = prefix_blocks(_chat("question " * 60, "answer " * 80, "follow-up " * 40))
    assert first
    assert later[: len(first)] == first
    assert len(later) > len(first)


def test_requests_sharing_a_system_prompt_share_its_blocks_and_then_diverge() -> None:
    left = prefix_blocks(_chat("about cats " * 50))
    right = prefix_blocks(_chat("about dogs " * 50))
    shared = len(SYSTEM) // BLOCK_CHARS
    assert left[:shared] == right[:shared]
    assert left[shared + 1 :] != right[shared + 1 :]


def test_a_block_hash_depends_on_everything_before_it() -> None:
    # The same block of text after a different system prompt is a different block.
    base = prefix_blocks(_chat("x" * 600, system="a" * BLOCK_CHARS))
    other = prefix_blocks(_chat("x" * 600, system="b" * BLOCK_CHARS))
    assert not set(base) & set(other)


def test_only_whole_blocks_are_hashed() -> None:
    assert prefix_blocks({"messages": [{"role": "user", "content": "short"}]}) == ()
    assert len(prefix_blocks({"prompt": "y" * (BLOCK_CHARS * 3 + 10)})) == 3


def test_tools_instructions_and_responses_input_are_part_of_the_prefix() -> None:
    plain = prompt_text({"input": "hello"})
    with_tools = prompt_text({"tools": [{"type": "function", "name": "f"}], "input": "hello"})
    with_instructions = prompt_text({"instructions": "be brief", "input": "hello"})
    assert plain != with_tools and with_tools.endswith(plain)
    assert with_instructions.endswith(plain) and "be brief" in with_instructions


def test_sampling_parameters_do_not_change_the_prefix() -> None:
    request = _chat("question " * 60)
    assert prefix_blocks(request) == prefix_blocks({**request, "temperature": 0.2, "seed": 9})


def test_the_work_per_request_is_bounded() -> None:
    assert len(prefix_blocks({"prompt": "z" * BLOCK_CHARS * 2000})) == 512


# --- the strategy --------------------------------------------------------------------------


def _pool(*ids: str, **options) -> tuple[BackendPool, BackendHealth, list[Backend]]:
    backends = [Backend(url=f"http://{i}", backend_id=i) for i in ids]
    health = BackendHealth(backends)
    strategy = PrefixAwareStrategy(health, **options)
    return BackendPool(backends, health, strategy=strategy), health, backends


def test_prefix_aware_is_a_published_strategy() -> None:
    assert "prefix_aware" in STRATEGIES
    single = [Backend(url="http://a", backend_id="a")]
    assert BackendPool(single, strategy="prefix_aware").uses_prefix
    assert not BackendPool(single).uses_prefix


def test_a_conversation_returns_to_the_backend_that_served_its_history() -> None:
    pool, _, _ = _pool("a", "b", "c", "d")
    first = pool.select(prefix=prefix_blocks(_chat("question " * 60)))
    for _ in range(5):
        # Unrelated traffic in between moves the rotation on.
        pool.select(prefix=prefix_blocks(_chat("noise " * 60, system="other " * 80)))
    follow_up = prefix_blocks(_chat("question " * 60, "answer " * 80, "more " * 40))
    assert pool.select(prefix=follow_up) == first


def test_without_a_match_it_behaves_like_least_in_flight() -> None:
    pool, health, backends = _pool("a", "b", "c")
    health.acquire(backends[0])
    health.acquire(backends[1])
    assert pool.select(prefix=()).backend_id == "c"
    assert pool.select(prefix=None).backend_id == "c"


def test_a_cached_backend_is_skipped_once_it_exceeds_the_load_bound() -> None:
    pool, health, backends = _pool("a", "b", "c", "d", load_factor=1.25)
    conversation = prefix_blocks(_chat("question " * 60))
    holder = pool.select(prefix=conversation)
    # Pile work onto the holder until it is past 1.25x the mean.
    for _ in range(4):
        health.acquire(holder)
    chosen = pool.select(prefix=conversation)
    assert chosen != holder
    # Once the holder drains, the conversation goes back to it.
    for _ in range(4):
        health.release(holder)
    assert pool.select(prefix=conversation) == holder


def test_a_shared_prefix_spreads_over_the_backends_that_hold_it_by_load() -> None:
    # A loose bound keeps both holders eligible, so load alone decides between them.
    pool, health, backends = _pool("a", "b", "c", "d", load_factor=10)
    shared = _chat("x", system="shared policy " * 80)
    # Two backends have served the shared system prompt.
    strategy = pool.strategy
    assert isinstance(strategy, PrefixAwareStrategy)
    strategy._record(prefix_blocks(shared), "a")
    strategy._record(prefix_blocks(shared), "b")
    health.acquire(backends[0])
    # Both hold it; the lighter one wins, and the empty c and d are not preferred.
    assert pool.select(prefix=prefix_blocks(shared)).backend_id == "b"


def test_the_deeper_match_wins_over_a_shared_shallow_one() -> None:
    pool, _, _ = _pool("a", "b")
    strategy = pool.strategy
    assert isinstance(strategy, PrefixAwareStrategy)
    strategy._record(prefix_blocks(_chat("q")), "a")
    strategy._record(prefix_blocks(_chat("question " * 60)), "b")
    matched = strategy.matched_blocks(prefix_blocks(_chat("question " * 60, "a", "b")), ["a", "b"])
    assert matched["b"] > matched["a"] > 0


def test_an_unhealthy_backend_is_never_chosen_even_when_it_holds_the_prefix() -> None:
    pool, health, backends = _pool("a", "b")
    conversation = prefix_blocks(_chat("question " * 60))
    holder = pool.select(prefix=conversation)
    for _ in range(3):
        health.record_failure(holder)
    assert pool.select(prefix=conversation) != holder


def test_the_index_is_bounded_and_evicts_the_least_recently_routed_blocks() -> None:
    pool, _, _ = _pool("a", "b", capacity_blocks=8)
    strategy = pool.strategy
    assert isinstance(strategy, PrefixAwareStrategy)
    old = prefix_blocks({"prompt": "o" * BLOCK_CHARS * 6})
    pool.select(prefix=old)
    pool.select(prefix=prefix_blocks({"prompt": "n" * BLOCK_CHARS * 6}))
    assert len(strategy) == 8
    assert strategy.matched_blocks(old, ["a", "b"]) == {"a": 0, "b": 0}


def test_forgetting_a_backend_drops_it_from_every_block() -> None:
    pool, _, _ = _pool("a", "b")
    strategy = pool.strategy
    assert isinstance(strategy, PrefixAwareStrategy)
    conversation = prefix_blocks(_chat("question " * 60))
    holder = pool.select(prefix=conversation).backend_id
    strategy.forget({holder})
    assert len(strategy) == 0


# --- through the gateway -------------------------------------------------------------------


def test_a_membership_change_keeps_the_index_for_the_hosts_that_stayed(control_plane) -> None:
    fleet = FleetUpstream()
    plane = _make_plane(fleet, control_plane)
    deployment = _registry(strategy="prefix_aware").resolve("launch-model", account_id=ACCOUNT_A)
    before = plane.pool_for(deployment)
    conversation = prefix_blocks(_chat("question " * 60))
    holder = before.select(prefix=conversation)

    added = Backend(url="http://c.test", backend_id="pod-c")
    grown = dataclasses.replace(deployment, backends=(*deployment.backends, added))
    after = plane.pool_for(grown)
    assert after is not before
    assert after.strategy is before.strategy
    assert after.select(prefix=conversation) == holder


async def test_conversation_turns_reach_the_same_host_without_a_session_key(
    control_plane, signing_key: SigningKey
) -> None:
    from fabric_data_plane.app import create_inference_app

    fleet = FleetUpstream()
    plane = _make_plane(fleet, control_plane)
    plane.registry = _registry(strategy="prefix_aware")
    app = create_inference_app(plane)
    transport = httpx.ASGITransport(app=app)
    auth = {"Authorization": f"Bearer {signing_key.issue()}"}
    async with httpx.AsyncClient(transport=transport, base_url="http://ingress.test") as http:
        hosts = []
        turns: list[str] = []
        for index in range(4):
            turns += [f"turn {index} " * 50, f"reply {index} " * 50]
            request = _chat(*turns[:-1])
            response = await http.post("/v1/chat/completions", json=request, headers=auth)
            assert response.status_code == 200
            hosts.append(fleet.requests[-1])
            # A different conversation in between: round-robin would alternate hosts.
            other = _chat(f"elsewhere {index} " * 50, system="another system " * 60)
            response = await http.post("/v1/chat/completions", json=other, headers=auth)
            assert response.status_code == 200
    assert len(set(hosts)) == 1


def test_the_registry_accepts_prefix_aware_from_configuration() -> None:
    registry = DeploymentRegistry.from_payload(
        {
            "deployments": [
                {
                    "deployment_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                    "account_id": str(ACCOUNT_A),
                    "model_alias": "launch-model",
                    "upstream_url": "http://model-host.test",
                    "strategy": "prefix_aware",
                }
            ]
        }
    )
    deployment = registry.resolve("launch-model", account_id=ACCOUNT_A)
    assert deployment.strategy == "prefix_aware"


def test_the_load_bound_comes_from_the_data_plane_settings(control_plane) -> None:
    fleet = FleetUpstream()
    plane = _make_plane(fleet, control_plane, prefix_routing_load_factor=1.5)
    deployment = _registry(strategy="prefix_aware").resolve("launch-model", account_id=ACCOUNT_A)
    pool = plane.pool_for(deployment)
    assert isinstance(pool.strategy, PrefixAwareStrategy)
    assert pool.strategy._load_factor == 1.5
    # A strategy change rebuilds the pool; the new strategy reads the same setting.
    plane.pool_for(dataclasses.replace(deployment, strategy="round_robin"))
    rebuilt = plane.pool_for(deployment)
    assert isinstance(rebuilt.strategy, PrefixAwareStrategy)
    assert rebuilt.strategy._load_factor == 1.5


def test_a_bound_of_one_never_lets_a_cached_backend_run_ahead_of_the_rest() -> None:
    pool, health, _ = _pool("a", "b", "c", "d", load_factor=1.0)
    conversation = prefix_blocks(_chat("question " * 60))
    holder = pool.select(prefix=conversation)
    health.acquire(holder)
    # The holder already carries one more request than the others: at 1.0 it must wait.
    assert pool.select(prefix=conversation) != holder


def test_benchmark_runs_label_prefix_aware_with_its_bound() -> None:
    from benchmarks.routing.harness import RunConfig
    from benchmarks.routing.workload import WorkloadConfig

    workload = WorkloadConfig()
    assert RunConfig("prefix_aware", workload, prefix_load_factor=1.1).label == "prefix_aware ×1.1"
    assert RunConfig("round_robin", workload).label == "round_robin"
