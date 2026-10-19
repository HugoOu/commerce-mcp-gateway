"""Step 4 的权重敏感性分析：手设权重是不是唯一结论？

对随机 Dirichlet 权重和 amount 权重 sweep 找每个权重下的最优阈值，报告：
  - 有多少组权重能在 11 个场景上做到 0 false accept / 0 false deny；
  - amount 权重至少多大时才存在完美阈值。
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "eval"))

from score_policy import WEIGHTS, load_scenarios

FEATURES = ("merchant", "tool", "amount", "budget", "expiry")


def _features(scenario):
    tool = 1.0 if scenario.tool_allowed and not scenario.requires_approval else 0.0
    return {
        "merchant": 1.0 if scenario.merchant == "m1" else 0.0,
        "tool": tool,
        "amount": 1.0 if scenario.amount <= 50.0 else 0.0,
        "budget": scenario.budget_score,
        "expiry": 1.0 if scenario.not_expired else 0.0,
    }


def _best(weights: dict[str, float], scenarios) -> tuple[int, float]:
    best_errors = 10**9
    best_threshold = 0.0
    for step in range(51):
        threshold = 0.5 + step / 100
        false_accept = false_deny = 0
        for scenario in scenarios:
            value = sum(weights[key] * _features(scenario)[key] for key in FEATURES)
            allowed = value >= threshold
            if allowed and scenario.expected == "deny":
                false_accept += 1
            if not allowed and scenario.expected == "allow":
                false_deny += 1
        errors = false_accept + false_deny
        if errors < best_errors:
            best_errors, best_threshold = errors, threshold
    return best_errors, best_threshold


def main() -> int:
    scenarios = load_scenarios()
    rng = random.Random(20261019)
    perfect = 0
    runs = 2000
    for _ in range(runs):
        raw = {key: rng.gammavariate(1.0, 1.0) for key in FEATURES}
        total = sum(raw.values())
        weights = {key: value / total for key, value in raw.items()}
        errors, _threshold = _best(weights, scenarios)
        if errors == 0:
            perfect += 1

    print(f"random_dirichlet={runs} perfect={perfect} perfect_share={perfect / runs:.3f}")
    base = dict(WEIGHTS)
    base_other = sum(base[key] for key in FEATURES if key != "amount")
    print("| amount_weight | errors | best_threshold |")
    print("| --- | --- | --- |")
    first_perfect = None
    for amount_weight in (0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.40, 0.50):
        weights = {
            key: (amount_weight if key == "amount" else base[key] / base_other * (1 - amount_weight))
            for key in FEATURES
        }
        errors, threshold = _best(weights, scenarios)
        print(f"| {amount_weight:.2f} | {errors} | {threshold:.2f} |")
        if errors == 0 and first_perfect is None:
            first_perfect = amount_weight
    print(f"first_perfect_amount_weight={first_perfect}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
