"""评测用的统计脚手架（W4 Step 3）——纯函数、零新依赖。

三件事：**精确区间**（Clopper–Pearson，二值响应）、**规则三**（0 事件时的速算上界）、
**bootstrap 分位区间**（延迟这类分布自由的量）。都自己写：scipy 不进依赖，而这三个加起来
不到 100 行。

口径见 `docs/evaluation.md` §0 与 §2b；本模块只负责**算**，不负责**选口径**。
"""

from __future__ import annotations

import json
import math
import pathlib
import random
from collections.abc import Callable, Sequence
from fractions import Fraction
from typing import Any

__all__ = [
    "bootstrap_ci",
    "clopper_pearson_upper",
    "fisher_exact_two_sided",
    "mean",
    "median",
    "quantile",
    "rule_of_three",
    "write_jsonl",
]


def _binom_cdf(k: int, n: int, p: float) -> float:
    """P(X ≤ k)，X ~ Binomial(n, p)。用 `math.comb` 精确算，不做正态近似。"""
    if p <= 0.0:
        return 1.0
    if p >= 1.0:
        return 0.0 if k < n else 1.0
    return sum(math.comb(n, i) * p**i * (1.0 - p) ** (n - i) for i in range(k + 1))


def clopper_pearson_upper(k: int, n: int, *, alpha: float = 0.05) -> float:
    """观测到 `k/n` 次成功时，成功率 p 的**单侧** `1-alpha` 上界（精确解，二分求根）。

    解的是 `P(X ≤ k | n, p) = alpha`；`k = 0` 时它有闭式解 `1 - alpha^(1/n)`。
    这个数是「0/30 → 9.5%」这类论断的来源，所以有专门的对照测试。
    """
    if n <= 0:
        raise ValueError("n 必须为正")
    if not 0 <= k <= n:
        raise ValueError("k 必须在 [0, n]")
    if k == n:
        return 1.0
    lo, hi = 0.0, 1.0
    for _ in range(200):  # 二分 200 次，精度远高于报告需要
        mid = (lo + hi) / 2.0
        if _binom_cdf(k, n, mid) > alpha:
            lo = mid  # mid 还在真值下方
        else:
            hi = mid
    return hi


def rule_of_three(n: int) -> float:
    """0 事件的速算上界 `≈ 3/n`（与精确解差在 5% 以内，用来交叉核对）。"""
    if n <= 0:
        raise ValueError("n 必须为正")
    return 3.0 / n


def fisher_exact_two_sided(a: int, b: int, c: int, d: int) -> float:
    """2×2 表的双侧 Fisher 精确检验 p 值。

    表：`[[a, b], [c, d]]`（行 = 两组，列 = 成功/失败）。固定边际后，成功数服从
    超几何分布；双侧 p 是「所有概率不大于观测表概率的表」的概率之和。

    用 `Fraction` 比较概率，避免浮点并列把某一项漏掉。样本量很小（评测里是 5–30），
    所以暴力枚举所有可行表完全够用。
    """
    for name, value in (("a", a), ("b", b), ("c", c), ("d", d)):
        if value < 0:
            raise ValueError(f"{name} 必须非负")
    n1 = a + b
    successes, total = a + c, a + b + c + d
    if total == 0:
        raise ValueError("总样本量为 0")

    def prob(x: int) -> Fraction:
        return Fraction(math.comb(successes, x) * math.comb(total - successes, n1 - x), math.comb(total, n1))

    low = max(0, n1 - (total - successes))
    high = min(n1, successes)
    observed = prob(a)
    return float(sum(prob(x) for x in range(low, high + 1) if prob(x) <= observed))


def mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def median(values: Sequence[float]) -> float:
    return quantile(values, 0.5)


def quantile(values: Sequence[float], q: float) -> float:
    """线性插值分位数（与 numpy 的默认口径一致）。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = q * (len(ordered) - 1)
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return ordered[int(position)]
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def bootstrap_ci(
    values: Sequence[float],
    *,
    statistic: Callable[[Sequence[float]], float] = mean,
    resamples: int = 10_000,
    alpha: float = 0.05,
    seed: int = 0,
) -> tuple[float, float]:
    """bootstrap 分位区间（percentile），分布自由——不假设正态。

    固定 `seed`，所以同一批数据重跑得到同一个区间（可复现是硬要求）。
    """
    if not values:
        return (0.0, 0.0)
    rng = random.Random(seed)
    n = len(values)
    draws = [statistic([values[rng.randrange(n)] for _ in range(n)]) for _ in range(resamples)]
    return (quantile(draws, alpha / 2), quantile(draws, 1 - alpha / 2))


def write_jsonl(path: pathlib.Path, rows: Sequence[dict[str, Any]]) -> pathlib.Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return path

