# Routing benchmark

> Routing strategies compared through the production gateway against simulated model hosts with a prefix cache and batch-dependent decode cost. Valid for comparing strategies with each other under this host model; not a measurement of any GPU, engine or deployment.

Commit `6c52f9580b60`, 4 backends, time scale 0.25. SLO: TTFT ≤ 500 ms and TPOT ≤ 60 ms.

Gateway overhead (not simulated, included in every TTFT below): 5.2 ms wall at p50, ≈ 21 ms on the simulated clock.

## shared_prefix

| Strategy | Rate (sessions/s) | Seeds | TTFT p50 ms | TTFT p95 ms | TPOT p95 ms | Cache hit | Load imbalance | Goodput | Failed |
|---|---|---|---|---|---|---|---|---|---|
| least_in_flight | 1 | 5 | 326 | 361 | 18.0 | 4.7% | 1.19 | 99.6% | 0 |
| least_in_flight | 2 | 5 | 327 | 399 | 18.7 | 12.7% | 1.07 | 98.1% | 0 |
| least_in_flight | 4 | 5 | 331 | 526 | 20.6 | 21.4% | 1.06 | 93.8% | 0 |
| prefix_aware ×1.1 | 1 | 5 | 324 | 343 | 18.2 | 19.8% | 1.16 | 99.6% | 0 |
| prefix_aware ×1.1 | 2 | 5 | 324 | 370 | 19.1 | 28.6% | 1.13 | 97.9% | 0 |
| prefix_aware ×1.1 | 4 | 5 | 310 | 441 | 21.4 | 45.7% | 1.07 | 96.2% | 0 |
| round_robin | 1 | 5 | 325 | 346 | 18.1 | 6.2% | 1.04 | 100.0% | 0 |
| round_robin | 2 | 5 | 326 | 348 | 18.8 | 12.0% | 1.02 | 100.0% | 0 |
| round_robin | 4 | 5 | 327 | 364 | 21.2 | 21.1% | 1.01 | 99.3% | 0 |
| session_affinity | 1 | 5 | 326 | 395 | 19.0 | 9.6% | 1.47 | 97.2% | 0 |
| session_affinity | 2 | 5 | 327 | 472 | 20.4 | 14.3% | 1.34 | 94.8% | 0 |
| session_affinity | 4 | 5 | 330 | 579 | 22.7 | 22.1% | 1.23 | 90.2% | 0 |
