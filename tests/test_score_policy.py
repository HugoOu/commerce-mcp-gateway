"""Step 4 对照实验：合取门 vs 分数规则。"""

from __future__ import annotations

import pytest
from score_policy import (
    WEIGHTS,
    conjunction,
    evaluate,
    load_scenarios,
    score,
    score_decision,
    sweep,
)


def test_conjunction_has_no_errors_on_labeled_scenarios() -> None:
    result = evaluate(load_scenarios(), conjunction)
    assert result == {"false_accept": 0, "false_deny": 0, "errors": 0}


def test_score_rule_has_no_perfect_threshold() -> None:
    rows = sweep(load_scenarios())
    assert rows and all(row["errors"] > 0 for row in rows)
    at_090 = next(row for row in rows if row["threshold"] == 0.90)
    at_091 = next(row for row in rows if row["threshold"] == 0.91)
    assert (at_090["false_accept"], at_090["false_deny"]) == (2, 0)
    assert (at_091["false_accept"], at_091["false_deny"]) == (0, 1)


def test_amount_is_the_compensable_signal() -> None:
    scenarios = {item.id: item for item in load_scenarios()}
    assert score(scenarios["amount_60"]) == pytest.approx(0.90)
    assert score(scenarios["legal_tight_budget"]) == pytest.approx(0.90)
    assert score_decision(scenarios["amount_60"], 0.90)
    assert score_decision(scenarios["legal_tight_budget"], 0.90)
    assert not score_decision(scenarios["amount_60"], 0.91)
    assert not score_decision(scenarios["legal_tight_budget"], 0.91)


def test_weights_are_normalized() -> None:
    assert sum(WEIGHTS.values()) == pytest.approx(1.0)
