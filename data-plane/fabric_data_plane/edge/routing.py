"""Choosing a stamp for a request.

Order of preference, for requests without affinity:

1. stamps not ejected for recent connection failures,
2. placements the stamp reported ready,
3. the preferred region (``X-Fabric-Region`` or the configured default),
4. fewest requests this edge has in flight to the stamp, ties broken at random.

Affinity overrides load. Agent runs, budgets and traces live in one stamp gateway's
memory, and prompt-prefix caches live on one stamp's model hosts, so requests carrying a
run id or a session key go to the same stamp every time. The stamp is chosen by
rendezvous (highest-random-weight) hashing over the candidates, which moves only the keys
of a stamp that leaves the set. A run id is strict: if its stamp cannot be reached the
request fails rather than silently starting a fresh run elsewhere. A session key is soft:
losing a warm cache is better than losing the request.
"""

from __future__ import annotations

import dataclasses
import hashlib
import random
import threading
import time
from typing import Any

from fabric_data_plane.edge.table import Placement


@dataclasses.dataclass
class _StampState:
    in_flight: int = 0
    consecutive_failures: int = 0
    ejected_until: float = 0.0
    ejections: int = 0


class StampHealth:
    """Passive health and in-flight counts, per stamp, for this edge process."""

    def __init__(self, *, failure_threshold: int, recovery_seconds: float) -> None:
        self._lock = threading.Lock()
        self._states: dict[str, _StampState] = {}
        self.failure_threshold = failure_threshold
        self.recovery_seconds = recovery_seconds

    def _state(self, stamp_id: str) -> _StampState:
        state = self._states.get(stamp_id)
        if state is None:
            state = self._states[stamp_id] = _StampState()
        return state

    def acquire(self, stamp_id: str) -> None:
        with self._lock:
            self._state(stamp_id).in_flight += 1

    def release(self, stamp_id: str) -> None:
        with self._lock:
            state = self._state(stamp_id)
            state.in_flight = max(0, state.in_flight - 1)

    def record_success(self, stamp_id: str) -> None:
        with self._lock:
            state = self._state(stamp_id)
            state.consecutive_failures = 0
            state.ejected_until = 0.0

    def record_failure(self, stamp_id: str) -> bool:
        """Count a connection failure; return True when it ejected the stamp."""
        with self._lock:
            state = self._state(stamp_id)
            state.consecutive_failures += 1
            if state.consecutive_failures >= self.failure_threshold:
                already = state.ejected_until > time.monotonic()
                state.ejected_until = time.monotonic() + self.recovery_seconds
                if not already:
                    state.ejections += 1
                    return True
            return False

    def available(self, stamp_id: str) -> bool:
        with self._lock:
            return self._state(stamp_id).ejected_until <= time.monotonic()

    def in_flight(self, stamp_id: str) -> int:
        with self._lock:
            return self._state(stamp_id).in_flight

    def snapshot(self) -> dict[str, dict[str, Any]]:
        now = time.monotonic()
        with self._lock:
            return {
                stamp_id: {
                    "in_flight": state.in_flight,
                    "consecutive_failures": state.consecutive_failures,
                    "ejected": state.ejected_until > now,
                    "ejected_for_seconds": round(max(0.0, state.ejected_until - now), 3),
                    "ejections": state.ejections,
                }
                for stamp_id, state in self._states.items()
            }


def _rendezvous_score(key: str, stamp_id: str) -> int:
    digest = hashlib.blake2b(f"{key}|{stamp_id}".encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big")


def rendezvous_order(key: str, candidates: list[Placement]) -> list[Placement]:
    """Candidates by descending rendezvous weight for ``key``; stable across processes."""
    return sorted(
        candidates,
        key=lambda item: _rendezvous_score(key, item.stamp.stamp_id),
        reverse=True,
    )


def order_candidates(
    candidates: list[Placement],
    health: StampHealth,
    *,
    region: str | None,
    affinity_key: str | None = None,
    strict_affinity: bool = False,
) -> list[Placement]:
    """Candidates in the order they should be tried.

    Ejected stamps go last rather than vanishing: when every candidate is ejected, trying
    one is better than refusing outright, and a success readmits it.
    """
    if not candidates:
        return []
    if affinity_key:
        ranked = rendezvous_order(affinity_key, candidates)
        if strict_affinity:
            return ranked[:1]
        return sorted(ranked, key=lambda item: not health.available(item.stamp.stamp_id))

    shuffled = list(candidates)
    random.shuffle(shuffled)
    return sorted(
        shuffled,
        key=lambda item: (
            not health.available(item.stamp.stamp_id),
            not item.ready,
            bool(region) and item.stamp.region != region,
            health.in_flight(item.stamp.stamp_id),
        ),
    )
