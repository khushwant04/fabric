"""Gateway metrics, including what the engine cannot see."""

from __future__ import annotations

from fabric_data_plane.metrics import Metrics


def test_a_refused_request_is_counted_where_the_engine_cannot_see_it() -> None:
    """A refusal never reaches a model host, so no engine metric records it.

    From the engine's side that traffic did not happen; from the caller's side it happened
    and failed. Only the gateway can report it, which is the reason this exists alongside
    the model server's own metrics.
    """
    metrics = Metrics()
    metrics.refused(deployment="dep-1", account="acct-1", reason="rate_limited")

    rendered = metrics.render()

    assert 'outcome="rate_limited"' in rendered
    assert 'deployment_id="dep-1"' in rendered


def test_latency_buckets_are_cumulative() -> None:
    """The exposition format requires each bucket to count everything at or below it."""
    metrics = Metrics()
    metrics.request_started("dep-1")
    metrics.request_finished(
        deployment="dep-1", account="acct-1", outcome="ok", duration_seconds=0.3
    )

    lines = [line for line in metrics.render().splitlines() if "_bucket{" in line]
    values = [int(line.rsplit(" ", 1)[1]) for line in lines]

    # Monotonic, and the largest bound holds every observation.
    assert values == sorted(values)
    assert values[-1] == 1


def test_in_flight_returns_to_zero() -> None:
    """A gauge that leaks makes a saturated gateway indistinguishable from a busy one."""
    metrics = Metrics()
    metrics.request_started("dep-1")
    metrics.request_finished(
        deployment="dep-1", account="acct-1", outcome="ok", duration_seconds=0.1
    )

    assert 'fabric_dp_requests_in_flight{deployment_id="dep-1"} 0' in metrics.render()
