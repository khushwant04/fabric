# Routing benchmark

> Routing strategies compared through the production gateway against simulated model hosts with a prefix cache and batch-dependent decode cost. Valid for comparing strategies with each other under this host model; not a measurement of any GPU, engine or deployment.

Commit `6c52f9580b60`, 4 backends, time scale 0.25. SLO: TTFT ≤ 500 ms and TPOT ≤ 60 ms.

Gateway overhead (not simulated, included in every TTFT below): 5.0 ms wall at p50, ≈ 20 ms on the simulated clock.

## multi_turn

| Strategy | Rate (sessions/s) | Seeds | TTFT p50 ms | TTFT p95 ms | TPOT p95 ms | Cache hit | Load imbalance | Goodput | Failed |
|---|---|---|---|---|---|---|---|---|---|
| least_in_flight | 1 | 5 | 127 | 342 | 19.9 | 72.8% | 1.08 | 99.9% | 0 |
| least_in_flight | 2 | 5 | 145 | 332 | 24.9 | 75.2% | 1.09 | 99.8% | 0 |
| least_in_flight | 4 | 5 | 282 | 1318 | 35.9 | 77.1% | 1.05 | 80.8% | 0 |
| prefix_aware ×1.1 | 1 | 5 | 78 | 331 | 20.3 | 85.4% | 1.14 | 99.5% | 0 |
| prefix_aware ×1.1 | 2 | 5 | 102 | 327 | 26.5 | 86.5% | 1.08 | 99.5% | 0 |
| prefix_aware ×1.1 | 4 | 5 | 306 | 1636 | 40.2 | 89.3% | 1.05 | 78.2% | 0 |
| round_robin | 1 | 5 | 129 | 340 | 20.3 | 71.9% | 1.00 | 99.9% | 0 |
| round_robin | 2 | 5 | 136 | 333 | 25.8 | 75.2% | 1.00 | 99.7% | 0 |
| round_robin | 4 | 5 | 234 | 1818 | 35.7 | 76.6% | 1.00 | 79.8% | 0 |
| session_affinity | 1 | 5 | 78 | 331 | 22.8 | 86.9% | 1.51 | 99.9% | 0 |
| session_affinity | 2 | 5 | 93 | 371 | 29.0 | 89.5% | 1.35 | 97.8% | 0 |
| session_affinity | 4 | 5 | 214 | 3593 | 36.9 | 91.2% | 1.22 | 76.2% | 0 |
