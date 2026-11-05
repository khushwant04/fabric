"""A simulated OpenAI-compatible model host with a prefix cache and a contended GPU.

The model is deliberately small and every constant is a parameter, because its job is to
make the *differences between routing decisions* visible, not to predict a real GPU:

* **Prefix cache.** Prompts are cut into fixed-size blocks, each identified by a hash of
  everything before it plus its own tokens, as paged KV caches identify blocks. A request
  reuses the longest run of leading blocks already cached and prefills only the rest.
  Blocks are evicted least-recently-used once the cache is full. A reply's blocks are
  cached as it is generated, so the next turn of the same conversation can hit them.
* **Prefill is compute-bound and serialized.** One prefill runs at a time, so prompts
  queue behind each other; this is what turns cache misses into time-to-first-token.
* **Decode slows with batch size.** Each step costs a base time plus a per-sequence
  increment for every sequence decoding concurrently.
* **Admission is bounded.** At most ``max_num_seqs`` requests are admitted; later ones
  wait for a slot, as a real engine's scheduler queue makes them wait.

The host reports ``usage.prompt_tokens_details.cached_tokens``, the field vLLM and the
OpenAI API use, and names itself in ``system_fingerprint`` so the client can see which
backend served each request without trusting the gateway to say.
"""

from __future__ import annotations

import asyncio
import collections
import dataclasses
import hashlib
import json
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx

#: Benchmark-only request field carrying the reply the host should "generate". The gateway
#: forwards unknown fields untouched, which is what lets the next turn's prompt contain the
#: same reply tokens the host cached.
REPLY_FIELD = "fabric_bench_reply_tokens"


@dataclasses.dataclass(frozen=True)
class HostModel:
    block_tokens: int = 16
    #: Prefix-cache capacity in blocks: 2048 x 16 = 32k tokens.
    cache_blocks: int = 2048
    #: Prefill throughput of 4k tokens/s. An assumption, not a measurement; vary it.
    prefill_ms_per_token: float = 0.25
    prefill_overhead_ms: float = 5.0
    #: The ~16 ms per output token measured for single-stream decode on the T4 deployment
    #: (README, "Earlier measured results"), so the decode scale matches real hardware.
    decode_ms_base: float = 16.0
    decode_ms_per_sequence: float = 1.0
    max_num_seqs: int = 16
    #: Tokens per streamed frame after the first. Fewer frames keep the gateway's own CPU
    #: cost out of the measurement; the first frame always carries one token.
    tokens_per_frame: int = 4


def _block_hashes(tokens: list[int], block_tokens: int) -> list[bytes]:
    hashes: list[bytes] = []
    previous = b""
    for start in range(0, len(tokens) - block_tokens + 1, block_tokens):
        block = ",".join(str(token) for token in tokens[start : start + block_tokens])
        previous = hashlib.blake2b(previous + block.encode(), digest_size=16).digest()
        hashes.append(previous)
    return hashes


def _prompt_tokens(payload: dict[str, Any]) -> list[int]:
    tokens: list[int] = []
    for message in payload.get("messages") or []:
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, str):
            tokens.extend(int(word[1:]) for word in content.split() if word.startswith("t"))
    return tokens


@dataclasses.dataclass
class HostStats:
    requests: int = 0
    prompt_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0
    peak_running: int = 0
    peak_waiting: int = 0


class SimulatedHost:
    """One backend. ``sleep`` advances simulated time, scaled by the caller."""

    def __init__(
        self, backend_id: str, model: HostModel, sleep: Callable[[float], Any]
    ) -> None:
        self.backend_id = backend_id
        self.model = model
        self._sleep = sleep
        self._cache: collections.OrderedDict[bytes, None] = collections.OrderedDict()
        self._prefill = asyncio.Lock()
        self._slots = asyncio.Semaphore(model.max_num_seqs)
        self._running = 0
        self._waiting = 0
        self.stats = HostStats()

    def _cached_prefix_blocks(self, hashes: list[bytes]) -> int:
        hit = 0
        for digest in hashes:
            if digest not in self._cache:
                break
            hit += 1
        return hit

    def _remember(self, hashes: list[bytes]) -> None:
        for digest in hashes:
            self._cache[digest] = None
            self._cache.move_to_end(digest)
        while len(self._cache) > self.model.cache_blocks:
            self._cache.popitem(last=False)

    def _step_seconds(self) -> float:
        model = self.model
        concurrent = max(1, self._running)
        return (model.decode_ms_base + model.decode_ms_per_sequence * (concurrent - 1)) / 1000

    def _frame(self, body: dict[str, Any]) -> bytes:
        return b"data: " + json.dumps(body, separators=(",", ":")).encode() + b"\n\n"

    async def stream(self, payload: dict[str, Any]) -> AsyncIterator[bytes]:
        model = self.model
        prompt = _prompt_tokens(payload)
        reply = [int(token) for token in payload.get(REPLY_FIELD) or []]
        output = max(1, int(payload.get("max_tokens") or len(reply) or 1))
        reply = (reply + [0] * output)[:output]
        report_usage = bool((payload.get("stream_options") or {}).get("include_usage"))

        self._waiting += 1
        self.stats.peak_waiting = max(self.stats.peak_waiting, self._waiting)
        admitted = False
        try:
            await self._slots.acquire()
            admitted = True
            self._waiting -= 1

            hashes = _block_hashes(prompt, model.block_tokens)
            async with self._prefill:
                # Read under the prefill lock: a prompt that waited may now find blocks a
                # request ahead of it just cached, as a real engine would.
                cached = self._cached_prefix_blocks(hashes) * model.block_tokens
                uncached = len(prompt) - cached
                await self._sleep(
                    (model.prefill_overhead_ms + uncached * model.prefill_ms_per_token) / 1000
                )
                self._remember(hashes)

            self.stats.requests += 1
            self.stats.prompt_tokens += len(prompt)
            self.stats.cached_tokens += cached
            self._running += 1
            self.stats.peak_running = max(self.stats.peak_running, self._running)
            try:
                sent = 0
                while sent < output:
                    count = 1 if sent == 0 else min(model.tokens_per_frame, output - sent)
                    await self._sleep(self._step_seconds() * count)
                    words = " ".join(f"t{token}" for token in reply[sent : sent + count])
                    sent += count
                    yield self._frame({
                        "id": "chatcmpl-bench",
                        "object": "chat.completion.chunk",
                        "system_fingerprint": self.backend_id,
                        "choices": [{
                            "index": 0,
                            "delta": {"content": words + " "},
                            "finish_reason": "length" if sent >= output else None,
                        }],
                    })
            finally:
                self._running -= 1
            # The generated reply's blocks join the cache, so the next turn, which resends
            # this reply as history, can reuse them.
            self._remember(_block_hashes(prompt + reply, model.block_tokens))
            self.stats.output_tokens += output

            if report_usage:
                yield self._frame({
                    "id": "chatcmpl-bench",
                    "object": "chat.completion.chunk",
                    "system_fingerprint": self.backend_id,
                    "choices": [],
                    "usage": {
                        "prompt_tokens": len(prompt),
                        "completion_tokens": output,
                        "total_tokens": len(prompt) + output,
                        "prompt_tokens_details": {"cached_tokens": cached},
                    },
                })
            yield b"data: [DONE]\n\n"
        finally:
            if admitted:
                self._slots.release()
            else:
                self._waiting -= 1


class _Stream(httpx.AsyncByteStream):
    def __init__(self, iterator: AsyncIterator[bytes]) -> None:
        self._iterator = iterator

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self._iterator:
            yield chunk

    async def aclose(self) -> None:
        # A gateway that stops reading (client hung up) must free the host's slot.
        await self._iterator.aclose()  # type: ignore[attr-defined]


class Fleet:
    """Simulated hosts behind one ``httpx`` transport, addressed by URL host name."""

    def __init__(self, hosts: list[SimulatedHost]) -> None:
        self.hosts = {host.backend_id: host for host in hosts}

    def url(self, backend_id: str) -> str:
        return f"http://{backend_id}.bench.test"

    async def handle(self, request: httpx.Request) -> httpx.Response:
        backend_id = request.url.host.removesuffix(".bench.test")
        host = self.hosts.get(backend_id)
        if host is None:
            return httpx.Response(404, json={"error": {"message": "unknown backend"}})
        payload = json.loads(request.content or b"{}")
        if not payload.get("stream"):
            return httpx.Response(400, json={"error": {"message": "benchmark hosts stream only"}})
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=_Stream(host.stream(payload)),
        )

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)
