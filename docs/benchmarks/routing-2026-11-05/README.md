# Routing benchmark artifacts, 2026-11-05

The evidence behind [ADR 0019](../../context/adrs/0019-prefix-aware-routing-with-bounded-load.md).
Each directory is one `python -m benchmarks.routing` sweep, run from `data-plane/`. Each
holds the versioned artifact (`results.json`, schema `fabric.routing-benchmark/v1`, with
the exact command, the commit and dirty flag, the host model, every workload seed and
each run's summary), the rendered `summary.md`, and its figures. The per-request
`requests.csv` files are left out for size; rerunning the recorded command regenerates
them.

| Directory | What it measures | Commit |
|---|---|---|
| `baseline/` | The four ADR 0011 strategies before prefix-aware routing existed; 2 seeds | `8fc7646` |
| `sens-multi_turn/`, `sens-mixed/` | Load bound sweep (1.0, 1.1, 1.25, 1.5) at 4 sessions/s; 5 seeds | `d7504ff` |
| `final-multi_turn/`, `final-mixed/` | All strategies at 1, 2 and 4 sessions/s, default bound 1.1; 5 seeds | `6c52f95` |
| `final-shared64/` | Single-turn requests over 64 system prompts, more than one host's cache | `6c52f95` |
| `final-nouser/` | Session affinity against prefix-aware routing without the OpenAI `user` field | `8bf0312` |

These are simulated model hosts behind the real gateway. Each artifact is marked
`citable_as_hardware_measurement: false`: it compares strategies under one host model and
says nothing about a GPU's absolute latency. The two `sens-*` sweeps ran concurrently, as
did the three `final-*` sweeps other than `final-nouser`. The gateway overhead each
artifact reports reflects that.
