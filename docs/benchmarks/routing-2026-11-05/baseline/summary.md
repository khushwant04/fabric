# Routing benchmark

> Routing strategies compared through the production gateway against simulated model hosts with a prefix cache and batch-dependent decode cost. Valid for comparing strategies with each other under this host model; not a measurement of any GPU, engine or deployment.

Commit `8fc7646820ab`, 4 backends, time scale 0.25. SLO: TTFT ≤ 500 ms and TPOT ≤ 60 ms.

Gateway overhead (not simulated, included in every TTFT below): 4.1 ms wall at p50, ≈ 16 ms on the simulated clock.

## mixed

| Strategy | Rate (sessions/s) | Seeds | TTFT p50 ms | TTFT p95 ms | TPOT p95 ms | Cache hit | Load imbalance | Goodput | Failed |
|---|---|---|---|---|---|---|---|---|---|
| least_in_flight | 0.5 | 2 | 152 | 366 | 17.9 | 60.0% | 1.26 | 100.0% | 0 |
| least_in_flight | 1 | 2 | 111 | 361 | 19.3 | 68.1% | 1.18 | 98.1% | 0 |
| least_in_flight | 2 | 2 | 112 | 350 | 22.0 | 75.2% | 1.14 | 99.2% | 0 |
| least_in_flight | 4 | 2 | 125 | 385 | 29.7 | 75.7% | 1.14 | 97.2% | 0 |
| round_robin | 0.5 | 2 | 157 | 431 | 18.0 | 56.8% | 1.02 | 99.0% | 0 |
| round_robin | 1 | 2 | 120 | 351 | 20.5 | 67.2% | 1.02 | 98.4% | 0 |
| round_robin | 2 | 2 | 113 | 340 | 22.8 | 73.2% | 1.00 | 98.9% | 0 |
| round_robin | 4 | 2 | 120 | 778 | 31.1 | 76.1% | 1.00 | 94.3% | 0 |
| session_affinity | 0.5 | 2 | 69 | 324 | 18.4 | 76.4% | 1.46 | 100.0% | 0 |
| session_affinity | 1 | 2 | 71 | 325 | 20.9 | 83.2% | 1.38 | 100.0% | 0 |
| session_affinity | 2 | 2 | 73 | 324 | 24.5 | 87.2% | 1.31 | 100.0% | 0 |
| session_affinity | 4 | 2 | 88 | 1458 | 32.3 | 90.4% | 1.29 | 92.3% | 0 |

## multi_turn

| Strategy | Rate (sessions/s) | Seeds | TTFT p50 ms | TTFT p95 ms | TPOT p95 ms | Cache hit | Load imbalance | Goodput | Failed |
|---|---|---|---|---|---|---|---|---|---|
| least_in_flight | 0.5 | 2 | 130 | 359 | 18.3 | 68.0% | 1.17 | 100.0% | 0 |
| least_in_flight | 1 | 2 | 116 | 327 | 19.8 | 73.6% | 1.05 | 100.0% | 0 |
| least_in_flight | 2 | 2 | 131 | 324 | 22.8 | 74.9% | 1.06 | 100.0% | 0 |
| least_in_flight | 4 | 2 | 162 | 348 | 30.9 | 77.1% | 1.06 | 99.3% | 0 |
| round_robin | 0.5 | 2 | 126 | 379 | 18.7 | 66.6% | 1.00 | 100.0% | 0 |
| round_robin | 1 | 2 | 117 | 331 | 20.4 | 73.3% | 1.00 | 100.0% | 0 |
| round_robin | 2 | 2 | 120 | 321 | 23.4 | 75.4% | 1.00 | 100.0% | 0 |
| round_robin | 4 | 2 | 145 | 354 | 32.3 | 76.6% | 1.00 | 98.1% | 0 |
| session_affinity | 0.5 | 2 | 70 | 326 | 19.9 | 83.8% | 1.82 | 99.5% | 0 |
| session_affinity | 1 | 2 | 73 | 322 | 22.8 | 87.0% | 1.62 | 99.5% | 0 |
| session_affinity | 2 | 2 | 75 | 324 | 24.9 | 89.0% | 1.30 | 99.6% | 0 |
| session_affinity | 4 | 2 | 94 | 1262 | 33.2 | 91.1% | 1.18 | 89.4% | 0 |

## shared_prefix

| Strategy | Rate (sessions/s) | Seeds | TTFT p50 ms | TTFT p95 ms | TPOT p95 ms | Cache hit | Load imbalance | Goodput | Failed |
|---|---|---|---|---|---|---|---|---|---|
| least_in_flight | 0.5 | 2 | 324 | 343 | 17.4 | 32.1% | 1.36 | 100.0% | 0 |
| least_in_flight | 1 | 2 | 74 | 332 | 17.9 | 53.6% | 1.21 | 100.0% | 0 |
| least_in_flight | 2 | 2 | 68 | 329 | 18.4 | 70.8% | 1.13 | 100.0% | 0 |
| least_in_flight | 4 | 2 | 67 | 331 | 19.8 | 81.8% | 1.12 | 99.3% | 0 |
| round_robin | 0.5 | 2 | 195 | 333 | 17.4 | 33.0% | 1.06 | 100.0% | 0 |
| round_robin | 1 | 2 | 73 | 339 | 18.1 | 54.9% | 1.05 | 100.0% | 0 |
| round_robin | 2 | 2 | 69 | 333 | 18.6 | 70.8% | 1.01 | 100.0% | 0 |
| round_robin | 4 | 2 | 66 | 323 | 19.9 | 81.8% | 1.01 | 100.0% | 0 |
| session_affinity | 0.5 | 2 | 207 | 335 | 18.1 | 33.0% | 1.80 | 100.0% | 0 |
| session_affinity | 1 | 2 | 76 | 340 | 19.0 | 52.5% | 1.49 | 97.6% | 0 |
| session_affinity | 2 | 2 | 69 | 337 | 20.0 | 70.8% | 1.29 | 98.7% | 0 |
| session_affinity | 4 | 2 | 69 | 327 | 21.9 | 81.8% | 1.16 | 99.0% | 0 |
