# Routing benchmark

> Routing strategies compared through the production gateway against simulated model hosts with a prefix cache and batch-dependent decode cost. Valid for comparing strategies with each other under this host model; not a measurement of any GPU, engine or deployment.

Commit `d7504ff072f3`, 4 backends, time scale 0.25. SLO: TTFT ≤ 500 ms and TPOT ≤ 60 ms.

Gateway overhead (not simulated, included in every TTFT below): 5.6 ms wall at p50, ≈ 22 ms on the simulated clock.

## multi_turn

| Strategy | Rate (sessions/s) | Seeds | TTFT p50 ms | TTFT p95 ms | TPOT p95 ms | Cache hit | Load imbalance | Goodput | Failed |
|---|---|---|---|---|---|---|---|---|---|
| least_in_flight | 4 | 5 | 269 | 1233 | 34.6 | 76.7% | 1.06 | 81.2% | 0 |
| prefix_aware ×1 | 4 | 5 | 279 | 1309 | 36.4 | 83.4% | 1.06 | 80.2% | 0 |
| prefix_aware ×1.1 | 4 | 5 | 294 | 1241 | 36.4 | 89.0% | 1.05 | 80.2% | 0 |
| prefix_aware ×1.25 | 4 | 5 | 233 | 1398 | 36.5 | 90.9% | 1.05 | 79.5% | 0 |
| prefix_aware ×1.5 | 4 | 5 | 211 | 1661 | 36.6 | 91.7% | 1.10 | 79.7% | 0 |
| session_affinity | 4 | 5 | 227 | 3561 | 36.0 | 91.2% | 1.22 | 75.9% | 0 |
