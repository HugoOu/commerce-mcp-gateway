"""Step 4 对照实验：合取门 vs 纯分数规则（只放 eval/，不进产品）。

合取门：所有硬条件必须同时满足。
分数规则：把五个信号加权求和，`score >= threshold` 就放行。

场景表在 `score_scenarios.yaml`；期望值来自 mandate，不是从分数反推的。
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass
from typing import Any

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCENARIOS = ROOT / "eval" / "score_scenarios.yaml"

WEIGHTS = {
    "merchant": 0.25,
    "tool": 0.20,
    "amount": 0.10,
    "budget": 0.25,
    "expiry": 0.20,
}
"""故意让 amount 权重最低：看分数规则能否用其他信号补偿一次超额。"""


@dataclass(frozen=True)
class Scenario:
    id: str
    amount: float
    merchant: str
    tool_allowed: bool
    requires_approval: bool
    budget_ok: bool
    budget_score: float
    not_expired: bool
    expected: str


def load_scenarios(path: pathlib.Path | None = None) -> list[Scenario]:
    raw = yaml.safe_load((path or SCENARIOS).read_text(encoding="utf-8"))
    rows = raw.get("scenarios") if isinstance(raw, dict) else None
    if not isinstance(rows, list) or not rows:
        raise ValueError("score_scenarios.yaml 必须有非空 scenarios")
    scenarios: list[Scenario] = []
    for row in rows:
        expected = str(row.get("expected"))
        if expected not in ("allow", "deny"):
            raise ValueError(f"{row['id']} 的 expected 只能是 allow/deny")
        scenarios.append(
            Scenario(
                id=str(row["id"]),
                amount=float(row["amount"]),
                merchant=str(row["merchant"]),
                tool_allowed=bool(row["tool_allowed"]),
                requires_approval=bool(row["requires_approval"]),
                budget_ok=bool(row["budget_ok"]),
                budget_score=max(0.0, min(1.0, float(row["budget_score"]))),
                not_expired=bool(row["not_expired"]),
                expected=expected,
            )
        )
    return scenarios


def conjunction(scenario: Scenario) -> bool:
    """ADR-0001 的合取门：任一硬条件不满足即拒绝。"""
    return all(
        (
            scenario.amount <= 50.0,
            scenario.merchant == "m1",
            scenario.tool_allowed,
            not scenario.requires_approval,
            scenario.budget_ok,
            scenario.not_expired,
        )
    )


def score(scenario: Scenario) -> float:
    """纯分数规则：没有硬门，所有信号都可互相补偿。"""
    tool = 1.0 if scenario.tool_allowed and not scenario.requires_approval else 0.0
    parts = {
        "merchant": 1.0 if scenario.merchant == "m1" else 0.0,
        "tool": tool,
        "amount": 1.0 if scenario.amount <= 50.0 else 0.0,
        "budget": scenario.budget_score,
        "expiry": 1.0 if scenario.not_expired else 0.0,
    }
    return sum(WEIGHTS[key] * value for key, value in parts.items())


def score_decision(scenario: Scenario, threshold: float) -> bool:
    return score(scenario) >= threshold


def evaluate(scenarios: list[Scenario], decide: Any) -> dict[str, int]:
    false_accept = false_deny = 0
    for scenario in scenarios:
        allowed = bool(decide(scenario))
        if allowed and scenario.expected == "deny":
            false_accept += 1
        if not allowed and scenario.expected == "allow":
            false_deny += 1
    return {
        "false_accept": false_accept,
        "false_deny": false_deny,
        "errors": false_accept + false_deny,
    }


def sweep(scenarios: list[Scenario], *, start: float = 0.5, stop: float = 1.0) -> list[dict[str, float]]:
    rows: list[dict[str, float]] = []
    for step in range(round((stop - start) * 100) + 1):
        threshold = round(start + step / 100, 2)
        result = evaluate(scenarios, lambda item, t=threshold: score_decision(item, t))
        rows.append({"threshold": threshold, **result})
    return rows


def perfect_thresholds(rows: list[dict[str, float]]) -> list[float]:
    return [row["threshold"] for row in rows if row["errors"] == 0]
