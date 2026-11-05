# Routing benchmark

> Routing strategies compared through the production gateway against simulated model hosts with a prefix cache and batch-dependent decode cost. Valid for comparing strategies with each other under this host model; not a measurement of any GPU, engine or deployment.

Commit `8bf0312a62c8`, 4 backends, time scale 0.25. SLO: TTFT ≤ 500 ms and TPOT ≤ 60 ms.

Gateway overhead (not simulated, included in every TTFT below): 4.7 ms wall at p50, ≈ 19 ms on the simulated clock.

## mixed

| Strategy | Rate (sessions/s) | Seeds | TTFT p50 ms | TTFT p95 ms | TPOT p95 ms | Cache hit | Load imbalance | Goodput | Failed |
|---|---|---|---|---|---|---|---|---|---|
| prefix_aware ×1.1 | 2 | 5 | 81 | 343 | 23.4 | 85.9% | 1.17 | 99.3% | 0 |
| session_affinity | 2 | 5 | 113 | 360 | 24.3 | 72.2% | 1.01 | 98.3% | 0 |

## multi_turn

| Strategy | Rate (sessions/s) | Seeds | TTFT p50 ms | TTFT p95 ms | TPOT p95 ms | Cache hit | Load imbalance | Goodput | Failed |
|---|---|---|---|---|---|---|---|---|---|
| prefix_aware ×1.1 | 2 | 5 | 88 | 327 | 25.0 | 86.4% | 1.07 | 99.6% | 0 |
| session_affinity | 2 | 5 | 137 | 330 | 25.8 | 75.0% | 1.00 | 99.6% | 0 |
