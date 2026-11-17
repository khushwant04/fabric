"""The pure routing-policy module: matching, the weighted draw, fallback chains, parsing."""

from __future__ import annotations

import hashlib
import pathlib
from collections import Counter
from typing import Any

import pytest

from fabric_data_plane import routing_policy as rp

A, B, C, D = "dep-a", "dep-b", "dep-c", "dep-d"
REPO = pathlib.Path(__file__).resolve().parents[2]


def policy(rules: list[dict[str, Any]] | None = None, **default: Any) -> rp.Policy:
    return rp.Policy.from_document(
        {
            "policy_id": "p",
            "account_id": "acct",
            "alias": "chat-default",
            "version": 3,
            "rules": rules or [],
            "default": {"id": "default", "targets": [{"deployment_id": A, "weight": 100}],
                        **default},
        }
    )


def rule(rule_id: str, match: dict[str, Any], *targets: tuple[str, int], **extra: Any):
    return {
        "id": rule_id,
        "match": match,
        "targets": [{"deployment_id": d, "weight": w} for d, w in targets],
        **extra,
    }


def facts(**values: Any) -> rp.RequestFacts:
    return rp.RequestFacts(**{"endpoint": "chat_completions", **values})


def test_the_two_copies_are_identical() -> None:
    control = REPO / "control-plane" / "app" / "core" / "routing_policy.py"
    if not control.exists():
        pytest.skip("control plane is not checked out next to the data plane")
    here = pathlib.Path(rp.__file__)
    assert hashlib.sha256(here.read_bytes()).digest() == hashlib.sha256(
        control.read_bytes()
    ).digest(), "the data-plane and control-plane routing_policy.py copies differ"


# --------------------------------------------------------------------------- matching


def test_first_matching_rule_wins_and_the_default_catches_the_rest() -> None:
    p = policy(
        [
            rule("tools", {"has_tools": True}, (B, 100)),
            rule("tools-too", {"has_tools": True}, (C, 100)),
        ]
    )
    assert p.evaluate(facts(has_tools=True)).id == "tools"
    assert p.evaluate(facts(has_tools=False)).id == "default"
    assert p.evaluate(facts()).is_default


@pytest.mark.parametrize(
    ("match", "hit", "miss"),
    [
        ({"api_key_ids": ["k1"]}, {"api_key_id": "k1"}, {"api_key_id": "k2"}),
        ({"principal_ids": ["sp"]}, {"principal_id": "sp"}, {"principal_id": None}),
        (
            {"headers": [{"name": "X-Tier", "op": "equals", "value": "gold"}]},
            {"headers": {"x-tier": "gold"}},
            {"headers": {"x-tier": "Gold"}},
        ),
        (
            {"headers": [{"name": "x-app", "op": "regex", "value": "^batch-"}]},
            {"headers": {"x-app": "batch-nightly"}},
            {"headers": {"x-app": "chat"}},
        ),
        ({"endpoints": ["messages"]}, {"endpoint": "messages"}, {"endpoint": "responses"}),
        ({"has_tools": False}, {"has_tools": False}, {"has_tools": True}),
        ({"prompt_tokens": {"min": 1000}}, {"prompt_tokens": 1000}, {"prompt_tokens": 999}),
        ({"prompt_tokens": {"max": 50}}, {"prompt_tokens": 50}, {"prompt_tokens": 51}),
        ({"max_tokens": {"min": 1, "max": 256}}, {"max_tokens": 256}, {"max_tokens": None}),
    ],
)
def test_each_condition(match, hit, miss) -> None:
    p = policy([rule("r", match, (B, 100))])
    assert p.evaluate(facts(**hit)).id == "r"
    assert p.evaluate(facts(**miss)).id == "default"


def test_conditions_are_anded() -> None:
    p = policy([rule("r", {"has_tools": True, "endpoints": ["responses"]}, (B, 100))])
    assert p.evaluate(facts(has_tools=True, endpoint="responses")).id == "r"
    assert p.evaluate(facts(has_tools=True)).id == "default"
    assert p.evaluate(facts(endpoint="responses")).id == "default"


def test_a_missing_header_never_matches() -> None:
    p = policy([rule("r", {"headers": [{"name": "x", "op": "regex", "value": ".*"}]}, (B, 100))])
    assert p.evaluate(facts(headers={})).id == "default"


# --------------------------------------------------------------------------- the draw


def split_rule() -> rp.Rule:
    return policy(
        [rule("split", {}, (A, 70), (B, 20), (C, 10))]
    ).rules[0]


def test_the_draw_is_deterministic_per_key() -> None:
    r = split_rule()
    for key in ("session:one", "session:two", "run:xyz"):
        first = rp.choose_target(r, key)
        assert all(rp.choose_target(r, key) == first for _ in range(20))


def test_the_draw_follows_the_weights() -> None:
    r = split_rule()
    counts = Counter(rp.choose_target(r, f"session:{index}") for index in range(20_000))
    assert abs(counts[A] / 20_000 - 0.70) < 0.02
    assert abs(counts[B] / 20_000 - 0.20) < 0.02
    assert abs(counts[C] / 20_000 - 0.10) < 0.015


def test_removing_a_target_that_did_not_win_never_changes_the_winner() -> None:
    """What lets a stamp holding only some targets agree with the edge."""
    r = split_rule()
    for index in range(2_000):
        key = f"route:{index}"
        winner = rp.choose_target(r, key)
        for missing in {A, B, C} - {winner}:
            available = {A, B, C} - {missing}
            assert rp.choose_target(r, key, available) == winner


def test_a_subset_is_renormalised() -> None:
    r = split_rule()
    assert rp.distribution(r, {B, C}) == {B: pytest.approx(66.6667, 1e-3),
                                          C: pytest.approx(33.3333, 1e-3)}
    counts = Counter(rp.choose_target(r, f"k{index}", {B, C}) for index in range(9_000))
    assert abs(counts[B] / 9_000 - 2 / 3) < 0.02
    assert rp.choose_target(r, "k", {D}) is None


def test_zero_weight_drains_a_target() -> None:
    r = policy([rule("r", {}, (A, 100), (B, 0))]).rules[0]
    assert {rp.choose_target(r, f"k{index}") for index in range(500)} == {A}
    assert rp.distribution(r) == {A: 100.0}


def test_changing_weights_moves_only_some_sessions() -> None:
    before = policy([rule("r", {}, (A, 90), (B, 10))]).rules[0]
    after = policy([rule("r", {}, (A, 80), (B, 20))]).rules[0]
    keys = [f"session:{index}" for index in range(5_000)]
    moved = [k for k in keys if rp.choose_target(before, k) != rp.choose_target(after, k)]
    # Only sessions moving from A to B, about a tenth of them, change.
    assert all(rp.choose_target(after, k) == B for k in moved)
    assert 0.05 < len(moved) / len(keys) < 0.15


# --------------------------------------------------------------------------- fallbacks


def test_attempt_chain_is_target_then_fallbacks() -> None:
    p = policy(fallbacks=[B, C], fallback_on=["server_error", "timeout"])
    decision = rp.decide(p, facts(routing_key="session:s"))
    assert decision.target == A
    assert decision.chain == (A, B, C)
    assert decision.rule.falls_back_on("timeout")
    assert not decision.rule.falls_back_on("overload")


def test_default_fallback_conditions() -> None:
    p = policy(fallbacks=[B])
    assert p.default.fallback_on == frozenset({"server_error", "no_ready_host"})


@pytest.mark.parametrize(
    ("targets", "fallbacks"),
    [([(A, 100)], [A]), ([(A, 50), (B, 50)], [C, B]), ([(A, 100)], [B, B])],
)
def test_cycles_are_refused(targets, fallbacks) -> None:
    with pytest.raises(rp.PolicyError, match="cycle"):
        policy([rule("r", {}, *targets, fallbacks=fallbacks)])


def test_without_a_key_simulation_reports_only_the_split() -> None:
    p = policy([rule("r", {}, (A, 50), (B, 50))])
    decision = rp.decide(p, facts())
    assert decision.target is None
    assert decision.distribution == {A: 50.0, B: 50.0}


# --------------------------------------------------------------------------- parsing


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"alias": "auto"}, "cannot name"),
        ({"default": None}, "default rule must be an object"),
        ({"default": {"id": "default", "targets": [{"deployment_id": A, "weight": 60}]}},
         "sum to 100"),
        ({"default": {"id": "default", "targets": []}}, "between 1 and"),
        ({"rules": [rule("r", {"headers": [{"name": "x", "op": "regex", "value": "("}]},
                         (A, 100))]}, "invalid regular expression"),
        ({"rules": [rule("r", {"endpoints": ["embeddings"]}, (A, 100))]}, "unknown endpoint"),
        ({"rules": [rule("default", {}, (A, 100))]}, "unique"),
        ({"rules": [rule("r", {}, (A, 100), strategy="round_robin")]}, "strategy"),
        ({"rules": [rule("r", {}, (A, 100), timeout_seconds=0)]}, "timeout"),
        ({"rules": [rule("r", {"prompt_tokens": {"min": 9, "max": 1}}, (A, 100))]},
         "greater than"),
    ],
)
def test_unenforceable_documents_are_refused(change, message) -> None:
    document = {
        "alias": "chat-default",
        "version": 1,
        "rules": [],
        "default": {"id": "default", "targets": [{"deployment_id": A, "weight": 100}]},
        **change,
    }
    with pytest.raises(rp.PolicyError, match=message):
        rp.Policy.from_document(document)


def test_a_bad_policy_is_skipped_and_the_rest_apply() -> None:
    good = {"account_id": "x", "alias": "good", "version": 1, "rules": [],
            "default": {"id": "default", "targets": [{"deployment_id": A, "weight": 100}]}}
    bad = {**good, "alias": "bad", "default": {"id": "default", "targets": []}}
    policies, errors = rp.PolicySet.from_documents([good, bad, "junk"], revision="r1")
    assert policies.get("x", "good") is not None
    assert policies.get("x", "bad") is None
    assert len(errors) == 2
    assert policies.aliases("x") == ["good"]
    assert policies.get("other-account", "good") is None


# --------------------------------------------------------------------------- request facts


def test_routing_key_order() -> None:
    assert rp.routing_key({"x-fabric-run-id": "r", "x-fabric-session": "s"}, None) == "run:r"
    assert rp.routing_key({"x-session-id": "s"}, {"user": "u"}) == "session:s"
    assert rp.routing_key({}, {"user": "u"}) == "session:u"
    assert rp.routing_key({"x-fabric-route-key": "abc"}, {}) == "route:abc"
    assert rp.routing_key({}, {}) is None


def test_facts_from_requests_of_each_shape() -> None:
    chat = rp.facts_from_request(
        endpoint="chat_completions",
        headers={"X-Tier": "gold", "X-Fabric-Session": "s1"},
        payload={
            "messages": [{"role": "user", "content": "x" * 400}],
            "tools": [{"type": "function"}],
            "max_tokens": 64,
        },
        api_key_id="k",
        principal_id="p",
    )
    assert chat.headers["x-tier"] == "gold"
    assert chat.has_tools and chat.max_tokens == 64 and chat.prompt_tokens == 100
    assert chat.routing_key == "session:s1"

    messages = rp.facts_from_request(
        endpoint="messages", headers={},
        payload={"system": "y" * 40, "messages": [{"role": "user", "content": [
            {"type": "text", "text": "z" * 40}]}], "max_tokens": 10},
        api_key_id=None, principal_id=None,
    )
    assert messages.prompt_tokens == 20 and not messages.has_tools

    responses = rp.facts_from_request(
        endpoint="responses", headers={},
        payload={"instructions": "a" * 8, "input": "b" * 8, "max_output_tokens": 5},
        api_key_id=None, principal_id=None,
    )
    assert responses.prompt_tokens == 4 and responses.max_tokens == 5


def test_endpoint_for_path() -> None:
    assert rp.endpoint_for_path("/v1/chat/completions") == "chat_completions"
    assert rp.endpoint_for_path("/v1/agent/runs") == "chat_completions"
    assert rp.endpoint_for_path("/v1/messages") == "messages"
    assert rp.endpoint_for_path("/v1/audio/transcriptions") is None
