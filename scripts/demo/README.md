# Feature demo

`feature_demo.py` runs short, self-checking demos against a live Fabric deployment through the
edge gateway and prints the numbers that show each feature working:

| Demo | What it shows |
|---|---|
| `auth` | API key exchanged for a short-lived inference token |
| `models` | Models the account can call, and the stamp the edge routed to |
| `prefix` | Prefix caching: a repeated long prompt prefix vs the same prompt made unique (cache defeated), time to first token and cached prompt tokens |
| `stream` | Time to first token and decode tokens/s |
| `load` | Concurrent streams: aggregate throughput, TTFT p50/p95 |
| `runs` | Agent run budgets: step and token counters, third call refused |
| `auto` | `model: "auto"` picks a deployment and reports why |
| `cascade` | `model: "cascade"` path and escalations (needs two or more chat deployments) |
| `guard` | Tool-call guard verdict on a request with tools |

Standard library only, Python 3.10+.

```bash
export FABRIC_API_KEY='fab_key_...'          # an account API key; never printed
python3 scripts/demo/feature_demo.py --model qwen3-coder
```

Useful options: `--only prefix,stream`, `--skip load`, `--prefix-words 4000` (a longer prefix
shows a bigger cache effect), `--repeats 8`, `--concurrency 16`, `--report demo.json`.
Defaults point at `https://api.nscale.host` and `https://inference.nscale.host/v1`; override with
`--control-url` / `--inference-url` or `FABRIC_CONTROL_URL` / `FABRIC_INFERENCE_URL`.
