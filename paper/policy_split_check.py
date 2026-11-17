"""Deterministic check of the routing-policy split, for the paper's evidence register.

Loads the data plane's pure routing-policy module (standard library only) by path and draws
targets for a fixed set of synthetic keys ("key-0" ... "key-N"). It reports

* the share each target receives against its weight,
* subset stability: for every key, a stamp that holds only some targets picks the same
  target as the full draw whenever it holds that target, and
* weight-change locality: raising one target's weight moves keys only onto that target.

No clock, randomness or network is involved, so the output is identical on every run. Run
from the repository root:

    python3 paper/policy_split_check.py
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MODULE = REPO / "data-plane" / "fabric_data_plane" / "routing_policy.py"
KEYS = 100_000


def load():
    spec = importlib.util.spec_from_file_location("routing_policy", MODULE)
    module = importlib.util.module_from_spec(spec)
    sys.modules["routing_policy"] = module
    spec.loader.exec_module(module)
    return module


def rule(rp, weights: dict[str, int]):
    return rp.Rule(
        id="split",
        name="split",
        match=None,
        targets=tuple(rp.Target(deployment_id=d, weight=w) for d, w in weights.items()),
    )


def main() -> None:
    rp = load()
    weights = {"a": 50, "b": 30, "c": 20}
    full = rule(rp, weights)
    keys = [f"key-{i}" for i in range(KEYS)]
    winners = [rp.choose_target(full, k) for k in keys]
    shares = {d: 100.0 * winners.count(d) / KEYS for d in weights}

    subsets = [{"a", "b"}, {"a", "c"}, {"b", "c"}]
    disagreements = 0
    checked = 0
    for subset in subsets:
        for key, winner in zip(keys, winners):
            if winner in subset:
                checked += 1
                if rp.choose_target(full, key, available=subset) != winner:
                    disagreements += 1

    canary_before = rule(rp, {"stable": 90, "canary": 10})
    canary_after = rule(rp, {"stable": 80, "canary": 20})
    moved = wrong_direction = 0
    for key in keys:
        before = rp.choose_target(canary_before, key)
        after = rp.choose_target(canary_after, key)
        if before != after:
            moved += 1
            if after != "canary":
                wrong_direction += 1

    print(
        json.dumps(
            {
                "keys": KEYS,
                "weights": weights,
                "shares_percent": {d: round(s, 2) for d, s in shares.items()},
                "subset_checks": checked,
                "subset_disagreements": disagreements,
                "canary_10_to_20_moved_percent": round(100.0 * moved / KEYS, 2),
                "canary_moves_not_onto_canary": wrong_direction,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
