"""统计脚手架（`eval/stats.py`）的单元测试——**对着已知值验**，不是对着实现验。

这些数字是 `docs/evaluation.md` §0 里样本量决策的依据，所以它们必须是对的：
`0/5 → 45.1%`、`0/30 → 9.5%`、`0/59 → 5.0%`、`0/60 → 4.9%`。
"""

from __future__ import annotations

import math

import pytest
from stats import (
    bootstrap_ci,
    clopper_pearson_upper,
    mean,
    median,
    quantile,
    rule_of_three,
)


@pytest.mark.parametrize(
    ("n", "expected"),
    [
        (5, 0.4507),    # 「0/5」与「真实 ASR 45%」不可区分
        (10, 0.2589),
        (20, 0.1391),
        (30, 0.0950),   # 主门槛：0/30 → ≤ 10%
        (59, 0.0495),
        (60, 0.0487),   # 主格加强：0/60 → ≤ 5%
    ],
)
def test_clopper_pearson_upper_for_zero_events(n: int, expected: float) -> None:
    assert clopper_pearson_upper(0, n) == pytest.approx(expected, abs=5e-4)
    # 闭式解与二分求根必须一致（两条独立路径互校）
    assert clopper_pearson_upper(0, n) == pytest.approx(1 - 0.05 ** (1 / n), abs=1e-9)


def test_rule_of_three_cross_checks_the_exact_bound() -> None:
    """规则三只是速算：在 n ≥ 20 时与精确解差 < 10%，小 n 时明显偏保守。"""
    for n in (20, 30, 60):
        assert rule_of_three(n) == pytest.approx(clopper_pearson_upper(0, n), rel=0.1)
    assert rule_of_three(5) > clopper_pearson_upper(0, 5)  # 0/5：60% vs 45%，规则三更保守


def test_upper_bound_shrinks_with_more_samples() -> None:
    bounds = [clopper_pearson_upper(0, n) for n in (5, 10, 20, 30, 60, 200)]
    assert bounds == sorted(bounds, reverse=True)


def test_more_events_widen_the_upper_bound() -> None:
    """同一个 n，观测到的成功越多，上界越高——单调性是这类区间的基本自检。"""
    assert clopper_pearson_upper(0, 30) < clopper_pearson_upper(1, 30) < clopper_pearson_upper(3, 30)


def test_full_success_has_upper_bound_one() -> None:
    assert clopper_pearson_upper(5, 5) == 1.0


def test_quantile_and_median() -> None:
    values = [1.0, 2.0, 3.0, 4.0]
    assert quantile(values, 0.0) == 1.0
    assert quantile(values, 1.0) == 4.0
    assert median(values) == pytest.approx(2.5)
    assert mean(values) == pytest.approx(2.5)


def test_bootstrap_ci_is_reproducible_and_ordered() -> None:
    values = [3.0, 3.1, 2.9, 3.2, 3.05, 2.95]
    low, high = bootstrap_ci(values, seed=7, resamples=2000)
    assert (low, high) == bootstrap_ci(values, seed=7, resamples=2000)  # 同种子同结果
    assert low <= mean(values) <= high
    assert low < high


def test_bootstrap_ci_of_a_constant_is_degenerate() -> None:
    low, high = bootstrap_ci([2.0] * 8, seed=1, resamples=500)
    assert math.isclose(low, 2.0) and math.isclose(high, 2.0)


def test_bad_inputs_are_rejected() -> None:
    with pytest.raises(ValueError):
        clopper_pearson_upper(0, 0)
    with pytest.raises(ValueError):
        clopper_pearson_upper(4, 3)
    with pytest.raises(ValueError):
        rule_of_three(0)

