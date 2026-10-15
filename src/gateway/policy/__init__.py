"""策略求值（ADR-0001）。

W3 Step 1 的**实现**由 AI 起草（你授权「Step 1 也请你完成」）；按 `AGENTS.md` 的双实现对照，
**你的版本仍待补**——diff 两版、把差异写进 `docs/diff-journal.md` 那一行仍然是你的。

语义约定（实现里逐条落地，出处见各自模块）：

1. 输入是**未校验的原始参数**（ADR-0014 的「校验类」），取值前必须自己判类型。
2. **默认 deny**：没有 allow 命中就是 `policy_denied`。
3. 策略**永远不能放宽授权**：先过 scope 门，再与 mandate 约束取交集（ADR-0020）。
4. 求值必须是**纯函数**（architecture 第 4 条）：时间由 `request.now` 传入，不偷读时钟。
5. 语言止步于**固定算子 + 段感知 glob**（ADR-0001）：不做通用 DSL、不做任意正则。
6. 「需要人」的规则返回 `require_approval`，而不是硬拒（ADR-0006 / 0021）。
"""

from __future__ import annotations

from typing import Protocol

from ..decision import AuthorizationBasis, Decision, InterceptedRequest
from .engine import YamlPolicyEngine, load_policy, segment_glob
from .schema import Binding, PolicyError, PolicyFile, Rule

__all__ = [
    "AllowAllPolicy",
    "PolicyEngine",
    "PolicyError",
    "PolicyFile",
    "Rule",
    "YamlPolicyEngine",
    "load_policy",
    "segment_glob",
]


class PolicyEngine(Protocol):
    """执行点只依赖这一个方法。"""

    def evaluate(self, request: InterceptedRequest, basis: AuthorizationBasis) -> Decision: ...


class AllowAllPolicy:
    """演示 / 测试用：全放行，**不做**授权门（连 scope 都不看）。

    只在没给策略文件时使用，`Gateway` 启动时会打印警告。生产必须给策略文件——ADR-0020 说
    「策略只能收紧授权」，而「全放行」连门都没有。
    """

    digest = "allow-all"

    def evaluate(self, request: InterceptedRequest, basis: AuthorizationBasis) -> Decision:
        return Decision(kind="allow")

    def binding_for(self, tool_name: str) -> Binding:
        return Binding()



