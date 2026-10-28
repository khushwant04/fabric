# Routing benchmark

Compares the gateway's routing strategies under realistic chat workloads. The gateway is
the production one: real token verification, limits, backend pool, strategy and streaming
meter, served by uvicorn and driven over HTTP. Only the model hosts are simulated, so a
result describes what a strategy actually decides.

```bash
cd data-plane
uv run --with-editable . --with matplotlib==3.9.2 python -m benchmarks.routing \
    --workloads multi_turn,shared_prefix,mixed --rates 0.5,1,2,4 --repeats 3
```

From the repository root, `make bench-routing` runs the same thing. A full sweep takes a
few minutes; `--duration` and `--time-scale` trade fidelity for speed.

## What is simulated

Each backend is a `SimulatedHost` (`simhost.py`):

| Behaviour | Model | Default |
|---|---|---|
| Prefix cache | 16-token blocks keyed by a hash of everything before them; longest cached prefix is reused; LRU eviction; generated replies are cached too | 2048 blocks (32k tokens) |
| Prefill | Serialized, cost proportional to uncached prompt tokens | 0.25 ms/token + 5 ms |
| Decode | Per step: base + increment per concurrently decoding sequence | 16 ms + 1 ms/sequence |
| Admission | At most `max_num_seqs` requests running; the rest queue | 16 |

The 16 ms decode step is the single-stream figure measured on the T4 deployment. The prefill
rate is an assumption; vary it with the host model rather than trusting it.

Hosts report `usage.prompt_tokens_details.cached_tokens`, the field vLLM and the OpenAI API
use, and identify themselves in `system_fingerprint`, so the load generator learns which
backend served a request and how much of its prompt was reused from the host itself.

## Workloads

`workload.py`, deterministic for a given seed:

- **`multi_turn`**: conversations of several turns sharing one of a few system prompts. Each
  turn resends the whole history, and the next turn waits for the reply plus think time.
- **`shared_prefix`**: single requests sharing a few long system prompts, as agents and RAG
  pipelines send.
- **`mixed`**: half single-turn, half conversations, with one reply in ten an order of
  magnitude longer than the rest. This is the case that punishes ignoring in-flight load.

Requests set the OpenAI `user` field to the session id unless `--no-user-field` is given.
Session affinity keys on it, so running both ways shows how much that strategy depends on
clients setting it.

## Outputs

`--out` (default `results/routing-<time>/`, ignored by git):

- `results.json`: versioned artifact (`fabric.routing-benchmark/v1`) with the commit and
  dirty flag, host model, every workload configuration and seed, the gateway calibration, and
  per-run summaries.
- `requests.csv`: every request's start, TTFT, latency, tokens, cached tokens and backend.
- `summary.md`: one table per workload.
- `figures/*.svg`: TTFT p95, cache-hit rate and SLO goodput against offered load, with the
  spread across seeds as error bars. Requires matplotlib.

Metrics: TTFT and TPOT percentiles (linear interpolation), throughput, cache-hit rate
(cached ÷ prompt tokens), load imbalance (busiest backend ÷ mean), and goodput: the
fraction of requests meeting both `--slo-ttft-ms` and `--slo-tpot-ms`.

## Limits

- Simulated time runs `--time-scale` times faster than wall time (0.25 by default), and the
  gateway's own processing is real, so it is scaled up with everything else. The calibration
  run measures it against hosts that take no time; the report states it beside the results.
  At the default scale it is about 3–4 ms wall, about 14 ms simulated, small next to a
  prefill but not zero.
- One prefill at a time and a linear decode cost are simplifications of chunked prefill and
  real batching. Compare strategies under the same host model; do not read absolute numbers
  as a GPU's.
- Results are labelled `citable_as_hardware_measurement: false`. A claim about real hardware
  needs the same workload replayed against a real host.
