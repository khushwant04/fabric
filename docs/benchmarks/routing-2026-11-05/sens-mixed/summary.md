# Routing benchmark

> Routing strategies compared through the production gateway against simulated model hosts with a prefix cache and batch-dependent decode cost. Valid for comparing strategies with each other under this host model; not a measurement of any GPU, engine or deployment.

Commit `d7504ff072f3`, 4 backends, time scale 0.25. SLO: TTFT ≤ 500 ms and TPOT ≤ 60 ms.

Gateway overhead (not simulated, included in every TTFT below): 5.4 ms wall at p50, ≈ 21 ms on the simulated clock.

## mixed

| Strategy | Rate (sessions/s) | Seeds | TTFT p50 ms | TTFT p95 ms | TPOT p95 ms | Cache hit | Load imbalance | Goodput | Failed |
|---|---|---|---|---|---|---|---|---|---|
| least_in_flight | 4 | 5 | 136 | 440 | 30.5 | 74.9% | 1.15 | 96.1% | 0 |
| prefix_aware ×1 | 4 | 5 | 110 | 376 | 30.8 | 83.2% | 1.10 | 98.2% | 0 |
| prefix_aware ×1.1 | 4 | 5 | 95 | 353 | 31.0 | 88.3% | 1.12 | 97.9% | 0 |
| prefix_aware ×1.25 | 4 | 5 | 94 | 376 | 30.8 | 90.3% | 1.07 | 97.3% | 0 |
| prefix_aware ×1.5 | 4 | 5 | 92 | 374 | 31.6 | 91.0% | 1.12 | 97.0% | 0 |
| session_affinity | 4 | 5 | 95 | 1723 | 32.4 | 90.4% | 1.27 | 90.5% | 0 |
