# ADR 0019: Prefix-aware routing, bounded by load

**Decision status:** Accepted  
**Implementation status:** Implemented in the data plane; accepted by control plane, CRD and agent  
**Date:** 2026-11-05

## Context

A model host keeps the KV cache of prompts it has computed. When the next request starts
with the same tokens (the next turn of a conversation, which resends its history, or a
request reusing a long system prompt), the engine skips that part of prefill. Prefill is
most of the time to first token for a long prompt, so where a request lands decides how
much work it costs.

ADR 0011's strategies ignore this. `least_in_flight` and `round_robin` spread turns of one
conversation across hosts, so most turns recompute their history. `session_affinity` keeps
a conversation on one host, but only when the client sends a session key, and it places
sessions by hash without looking at load.

The routing benchmark (`data-plane/benchmarks/routing`) measured both failure modes
through the production gateway against simulated hosts with a prefix cache:

- At 4 sessions/s of multi-turn chat, `least_in_flight` reused 75–77% of prompt tokens and
  session affinity 84–91%.
- Session affinity's hashing put 192 requests on one host and 128–136 on the others. That
  host hit its batch limit with 10 requests waiting. Its p95 time to first token rose from
  about 350 ms (least in flight) to 1.0–1.5 s, and goodput fell to 87–92%.

## Decision

Add a fifth strategy, `prefix_aware`, selected per deployment like the others.

**What the gateway knows.** The gateway cannot see an engine's cache and does not
tokenize. It approximates the cache instead:

- The request's prompt (tool definitions, `instructions`, then `messages`, `input` or
  `prompt`) is serialized in a prefix-preserving form. Each message is encoded on its
  own, so appending a message never changes the encoding of the ones before it.
  Sampling parameters are left out; they change the output, not the prompt.
- The encoding is cut into 256-byte blocks (about 64 tokens of English, the order of an
  engine's KV page). Each block is identified by a BLAKE2b hash chained over every block
  before it. Two prompts share their first `n` hashes exactly when they share their first
  `n` blocks. A trailing partial block is not hashed, and at most 512 blocks are.
- A bounded LRU index (65,536 blocks per pool) maps each block hash to the four backends
  that most recently served it. Every routed request refreshes its blocks.

**How it chooses.**

1. *Load bound.* A backend is eligible if its in-flight count, counting this request, is
   at most `ceil(load_factor × mean)` over the healthy candidates. This is consistent
   hashing with bounded loads. The least loaded backend always qualifies.
2. *Longest match.* Among eligible backends, pick the one whose index entries cover the
   longest run of the request's leading blocks.
3. *Ties and misses.* Ties go to the least loaded backend, rotating among equals. With no
   match at all, this is least-in-flight.

Health applies first, as for every strategy (ADR 0011), so an ejected backend is never a
candidate however much of the prompt it holds.

**The bound is a setting.** `FABRIC_DP_PREFIX_ROUTING_LOAD_FACTOR`, from 1.0 to 4.0,
default **1.1**. It is a data-plane setting rather than a per-deployment field because it
describes the gateway's tolerance for imbalance, not the model. The default was chosen by
measurement; see Evaluation.

**Membership changes keep the index.** When a deployment's backends change, the pool is
rebuilt, but a prefix-aware pool keeps its strategy and forgets only the backends that
left. The hosts that stayed still hold their caches.

**The vocabulary.** `prefix_aware` is accepted by the control plane's deployment spec,
the CRD enum, and the agent's strategy filter, and flows to the data plane like the other
strategies. An older data plane that does not know the name falls back to
`least_in_flight` (ADR 0011).

## Evaluation

All numbers come from the routing benchmark: the production gateway, served by uvicorn,
in front of four simulated hosts. Each host has a 32k-token prefix cache with LRU
eviction, serialized prefill at 0.25 ms per uncached token, decode at 16 ms plus 1 ms per
running sequence, and at most 16 running sequences. They compare strategies with each
other under this host model. They are not measurements of a GPU (`citable_as_hardware_measurement: false`).
The SLO is a time to first token (TTFT) of at most 500 ms and a time per output token of
at most 60 ms; goodput is the fraction of requests meeting both.

### Choosing the load bound

At 4 sessions/s, 5 seeds each. For multi-turn chat, seeds 3 and 4 saturated the fleet
for every strategy (goodput 50–61% even for least in flight), so they are excluded: no
routing choice can fix a fleet that is out of capacity.

| Strategy | Multi-turn TTFT p50 / p95 / p99 (ms) | Cache hits | Mixed TTFT p50 / p95 / p99 (ms) | Cache hits | Mixed goodput |
|---|---|---|---|---|---|
| `least_in_flight` | 170 / 375 / 590 | 77.0% | 136 / 440 / 761 | 74.9% | 96.1% |
| `session_affinity` | 106 / 1138 / 1805 | 91.1% | 95 / 1723 / 2856 | 90.4% | 90.5% |
| `prefix_aware` ×1.0 | 145 / 396 / 566 | 83.4% | 110 / 376 / 623 | 83.2% | 98.2% |
| **`prefix_aware` ×1.1** | **117 / 366 / 594** | **88.6%** | **95 / 353 / 608** | **88.3%** | **97.9%** |
| `prefix_aware` ×1.25 | 109 / 346 / 734 | 90.8% | 94 / 376 / 819 | 90.3% | 97.3% |
| `prefix_aware` ×1.5 | 103 / 361 / 754 | 91.6% | 92 / 374 / 1368 | 91.0% | 97.0% |

At 1.1, prefix-aware routing kept 88% of prompt tokens cached against affinity's 91%. It
cut median TTFT by 30% against least in flight and matched least in flight's p99. It beat
least in flight's p95 on all five mixed-traffic seeds. Looser bounds bought two or three
points of cache hits for a p99 up to 1.8 times worse, which is the failure this strategy
exists to avoid. A bound of 1.0 is as even as least in flight and gives up a third of the
cache benefit.

### Across load

<!-- FINAL-SWEEP -->

## Consequences

### Positive

- Conversations and shared system prompts reuse the host's cache without the client
  sending a session key. The `user` field and session headers are not read.
- The tail latency stays that of load-based routing, because cache affinity can never
  push a host far past the mean.
- The cost per request is one BLAKE2b pass over at most 128 KB of prompt and a dictionary
  walk, both bounded.

### Negative

- The index is a guess. It does not know what an engine actually evicted, so it can send a
  request to a host that has since dropped the prefix. The request is then just a cache
  miss, served normally.
- Like health and in-flight counts (ADR 0011), the index is process-local. Several
  gateway replicas each learn their own index. Each one still routes a conversation
  consistently if its requests reach the same replica.
- Blocks are cut on bytes, not tokens, so the matched length is approximate.
- In-flight count is the load signal. A host's real capacity, its batch limit and KV
  memory, is not visible to the gateway. The bound limits imbalance rather than
  guaranteeing headroom.

## Alternatives considered

- **Tokenize in the gateway and hash token blocks exactly as the engine does.** Rejected for
  now: it needs the model's tokenizer in the data plane for every deployment, and the
  approximation already captures the shared prefixes that matter.
- **Ask the engine what it has cached.** No standard API exists across engines, and
  polling would add a dependency on the hosts' responsiveness to routing.
- **Session affinity with a load fallback.** It still needs a client-supplied key, and it
  cannot share a system prompt across sessions.
- **No load bound (pure longest match).** This reproduces session affinity's hot-spotting
  for popular prefixes.

## Verification

- `tests/test_prefix_routing.py` covers the hashing and the strategy:
  - prefix preservation across turns, and the chaining of block hashes;
  - tools and responses input in the prefix, sampling parameters left out, bounded work;
  - return to the cached host, fallback to least in flight, and the load bound;
  - spreading by load among the holders of a shared prefix, and the deepest match winning;
  - exclusion of unhealthy holders, index eviction, and forgetting departed backends;
  - the index surviving a membership change, and the setting reaching the strategy;
  - a four-turn conversation through the real gateway reaching one host without a
    session key.
- The agent test carries every strategy name; the control plane accepts the new value
  and `openapi.json` is regenerated.
- `make bench-routing` reproduces the evaluation. `--prefix-load-factors` sweeps the
  bound.
