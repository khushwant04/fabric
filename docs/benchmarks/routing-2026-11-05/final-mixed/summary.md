# Routing benchmark

> Routing strategies compared through the production gateway against simulated model hosts with a prefix cache and batch-dependent decode cost. Valid for comparing strategies with each other under this host model; not a measurement of any GPU, engine or deployment.

Commit `6c52f9580b60`, 4 backends, time scale 0.25. SLO: TTFT ≤ 500 ms and TPOT ≤ 60 ms.

Gateway overhead (not simulated, included in every TTFT below): 4.8 ms wall at p50, ≈ 19 ms on the simulated clock.

## mixed

| Strategy | Rate (sessions/s) | Seeds | TTFT p50 ms | TTFT p95 ms | TPOT p95 ms | Cache hit | Load imbalance | Goodput | Failed |
|---|---|---|---|---|---|---|---|---|---|
| least_in_flight | 1 | 5 | 111 | 407 | 19.7 | 67.2% | 1.28 | 97.4% | 0 |
| least_in_flight | 2 | 5 | 116 | 347 | 22.6 | 73.5% | 1.16 | 98.5% | 0 |
| least_in_flight | 4 | 5 | 144 | 442 | 31.8 | 75.8% | 1.15 | 96.0% | 0 |
| prefix_aware ×1.1 | 1 | 5 | 78 | 338 | 19.8 | 83.8% | 1.26 | 99.5% | 0 |
| prefix_aware ×1.1 | 2 | 5 | 82 | 351 | 22.7 | 85.5% | 1.24 | 99.0% | 0 |
| prefix_aware ×1.1 | 4 | 5 | 115 | 494 | 33.1 | 88.6% | 1.14 | 96.1% | 0 |
| round_robin | 1 | 5 | 127 | 381 | 20.3 | 65.2% | 1.01 | 97.6% | 0 |
| round_robin | 2 | 5 | 116 | 367 | 23.9 | 71.9% | 1.01 | 98.5% | 0 |
| round_robin | 4 | 5 | 145 | 997 | 31.7 | 74.3% | 1.00 | 93.0% | 0 |
| session_affinity | 1 | 5 | 79 | 337 | 21.2 | 82.9% | 1.41 | 99.5% | 0 |
| session_affinity | 2 | 5 | 82 | 335 | 26.7 | 87.5% | 1.37 | 98.7% | 0 |
| session_affinity | 4 | 5 | 101 | 1746 | 32.9 | 90.4% | 1.27 | 89.2% | 0 |
