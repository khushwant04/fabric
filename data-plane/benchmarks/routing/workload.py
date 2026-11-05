"""Deterministic request workloads for the routing benchmark.

A prompt is a sequence of token words, ``t<id>``, joined by spaces. Shared context is
literally identical text: every request that uses one system prompt starts with the same
words, and each turn of a conversation repeats the whole history before the new message.
That is what makes prefix caching measurable, because a simulated host keys its cache on
exactly those tokens, as a real engine keys it on token ids.

Everything is drawn from one seeded generator, so a workload is a pure function of its
configuration: two runs with the same seed send the same requests at the same offsets.
"""

from __future__ import annotations

import dataclasses
import math
import random
from collections.abc import Iterator

WORKLOADS = ("multi_turn", "shared_prefix", "mixed")


@dataclasses.dataclass(frozen=True)
class Message:
    role: str
    tokens: tuple[int, ...]

    def as_openai(self) -> dict[str, str]:
        return {"role": self.role, "content": " ".join(f"t{token}" for token in self.tokens)}


@dataclasses.dataclass(frozen=True)
class Turn:
    """One request: what to send, when, and how long a reply the simulated host gives."""

    #: Seconds after the start of the run, on the simulated clock.
    offset: float
    session_id: str
    messages: tuple[Message, ...]
    output_tokens: int
    #: The session's next turn waits for this one to finish, plus this think time.
    next_turn: Turn | None = None
    think_seconds: float = 0.0

    @property
    def prompt_tokens(self) -> int:
        return sum(len(message.tokens) for message in self.messages)


@dataclasses.dataclass(frozen=True)
class WorkloadConfig:
    kind: str = "multi_turn"
    #: Sessions (or single requests) started per simulated second, as a Poisson process.
    rate: float = 2.0
    duration_seconds: float = 60.0
    seed: int = 0
    #: Distinct system prompts; requests pick one, so several share each.
    system_prompts: int = 4
    system_prompt_tokens: int = 1024
    turns_per_session: int = 4
    user_message_tokens: int = 96
    mean_output_tokens: int = 128
    think_seconds: float = 2.0
    #: Whether requests carry the OpenAI ``user`` field. Session affinity keys on it, so
    #: running both ways shows how much that strategy depends on clients setting it.
    set_user_field: bool = True

    def __post_init__(self) -> None:
        if self.kind not in WORKLOADS:
            raise ValueError(f"unknown workload {self.kind!r}; choose from {WORKLOADS}")
        if self.rate <= 0 or self.duration_seconds <= 0:
            raise ValueError("rate and duration must be positive")


class _Tokens:
    """Fresh token ids, so unrelated text never collides with shared text by accident."""

    def __init__(self) -> None:
        self._next = 0

    def take(self, count: int) -> tuple[int, ...]:
        start = self._next
        self._next += count
        return tuple(range(start, start + count))


def _output_length(rng: random.Random, mean: int, *, heavy_tail: bool) -> int:
    if heavy_tail and rng.random() < 0.1:
        # One request in ten is a long generation, the case that makes request counts a
        # poor proxy for load and punishes a strategy that ignores in-flight work.
        return max(1, int(rng.expovariate(1 / (mean * 8))))
    # Lognormal around the mean: most replies are short, a few are several times longer.
    sigma = 0.6
    mu = math.log(mean) - sigma * sigma / 2
    return max(1, int(rng.lognormvariate(mu, sigma)))


def _arrivals(rng: random.Random, rate: float, duration: float) -> Iterator[float]:
    clock = rng.expovariate(rate)
    while clock < duration:
        yield clock
        clock += rng.expovariate(rate)


def generate(config: WorkloadConfig) -> list[Turn]:
    """Return the first turn of every session, ordered by start time.

    Later turns hang off ``next_turn``: they are released only when the previous one
    finishes, which is how a person waiting for a reply behaves, and which an open-loop
    schedule of every turn would get wrong under load.
    """
    rng = random.Random(config.seed)
    tokens = _Tokens()
    system_prompts = [
        Message("system", tokens.take(config.system_prompt_tokens))
        for _ in range(config.system_prompts)
    ]
    heavy_tail = config.kind == "mixed"
    sessions: list[Turn] = []
    for index, start in enumerate(_arrivals(rng, config.rate, config.duration_seconds)):
        session_id = f"session-{config.seed}-{index}"
        system = system_prompts[rng.randrange(len(system_prompts))]
        if config.kind == "shared_prefix":
            turns = 1
        elif config.kind == "mixed":
            turns = 1 if rng.random() < 0.5 else config.turns_per_session
        else:
            turns = config.turns_per_session

        history: list[Message] = [system]
        planned: list[tuple[tuple[Message, ...], int, float]] = []
        for _ in range(turns):
            history.append(Message("user", tokens.take(config.user_message_tokens)))
            output = _output_length(rng, config.mean_output_tokens, heavy_tail=heavy_tail)
            planned.append((tuple(history), output, rng.expovariate(1 / config.think_seconds)))
            # The reply becomes part of the next turn's prompt, as a chat client resends it.
            history.append(Message("assistant", tokens.take(output)))

        following: Turn | None = None
        for messages, output, think in reversed(planned):
            following = Turn(
                offset=start,
                session_id=session_id,
                messages=messages,
                output_tokens=output,
                next_turn=following,
                think_seconds=think,
            )
        assert following is not None
        sessions.append(following)
    return sessions


def count_turns(sessions: list[Turn]) -> int:
    total = 0
    for first in sessions:
        turn: Turn | None = first
        while turn is not None:
            total += 1
            turn = turn.next_turn
    return total
