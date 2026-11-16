# Measured results

## Deployed validation snapshot

From an earlier deployed validation snapshot. Full context, including what these numbers do not show, is in the [project review](project-review.md); do not treat them as benchmarks of the current five-node topology.

| Observation | Value |
|---|---|
| Weights load from node-local NVMe | 4.25 GiB in 3.3 s |
| Cold start to first healthy response | ~510 s, mostly graph compilation |
| Single-stream decode | ~16 ms per token (~63 tokens/s) |
| Time to first token (p95) | 1.6 s |
| Concurrency cap | 30 parallel requests → 5 served, 25 refused `429` with `Retry-After` |
| GPU profile detected | `Tesla T4`, compute capability 7.5, 16384 MiB |

## The decode kernel

A fused single-token gated-delta kernel replacing the one vLLM calls, doing QKV unpacking,
normalisation, gate derivation, the recurrence, and state scatter in one launch.

| Measured inside a CUDA graph, T4, launch-model shapes | vLLM | Fabric | |
|---|---|---|---|
| 1 sequence | 20.37 µs | 11.30 µs | **1.81x** |
| 4 sequences | 33.59 µs | 26.77 µs | 1.06x |
| 16 and 32 sequences | — | — | parity |

Output and next recurrent state are **bit-identical** to vLLM's kernel at every batch size
tested.

**What is not claimed:** that the platform serves tokens faster. This operation is roughly 2%
of a token's cost, so eliminating it entirely would move end-to-end throughput by about that
much, and no end-to-end advantage was demonstrated. Parity at higher batch is what the
arithmetic requires: the step rewrites the whole recurrent state each token, about 2 MB at
these shapes, so both implementations meet the same bandwidth limit.
