"""策略求值（ADR-0001）——**本模块由你手写**（`AGENTS.md` 分工表）。

Step 0 只冻结接口与语义；下面的 `YamlPolicyEngine` 是 stub，等你填实现。

语义约定（写实现时必须满足，逐条都有出处）：

1. 输入是**未校验的原始参数**（ADR-0014 的「校验类」），所以取值前要自己校验类型。
2. **默认 deny**：没有任何 allow 规则命中就是 `Decision(kind="deny", reason_code="policy_denied")`。
3. 策略**永远不能放宽授权**：结果要与 `basis.scopes` 取交集（ADR-0020），策略只能收紧。
4. 求值必须是**纯函数**（architecture 第 4 条）：时间、随机等非确定输入由调用方传入或记录，
   否则 I4 的重放不可复算。
5. 语言止步于**固定算子 + URI 段感知 glob**（ADR-0001）：不引入通用 DSL、不做任意正则。
6. 命中「需要人」的规则时返回 `require_approval`，而不是硬拒（ADR-0006 / 0021）。
"""

from __future__ import annotations

import pathlib
from typing import Protocol

from ..decision import AuthorizationBasis, Decision, InterceptedRequest

__all__ = ["PolicyEngine", "YamlPolicyEngine", "load_policy"]


class PolicyEngine(Protocol):
    """执行点只依赖这一个方法。"""

    def evaluate(self, request: InterceptedRequest, basis: AuthorizationBasis) -> Decision: ...


class YamlPolicyEngine:
    """stub：W3 Step 1 由你实现（YAML → pydantic → 内存决策结构 + 固定算子）。"""

    def __init__(self, rules: dict) -> None:
        self.rules = rules

    def evaluate(self, request: InterceptedRequest, basis: AuthorizationBasis) -> Decision:
        raise NotImplementedError("W3 Step 1：策略求值由你实现（AGENTS.md 分工表）")


def load_policy(path: pathlib.Path) -> PolicyEngine:
    """读 YAML 策略文件并构造引擎。stub：等 Step 1 实现。"""
    raise NotImplementedError("W3 Step 1：YAML 加载由你实现（AGENTS.md 分工表）")

