"""共享词汇：被拦截的请求、授权依据、`Decision`、mandate。

放在一个模块里有两个原因：① 避免 policy / budget / evidence / enforce 之间互相 import；
② 让「一个 Decision 由什么构成」只有一处定义（architecture 第 3 条）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

DecisionKind = Literal["allow", "deny", "require_approval", "transform"]


@dataclass(frozen=True)
class Mandate:
    """用户意图（结构对齐 AP2 的 open / closed 两阶段，见 `docs/mock-world.md` §2）。

    ADR-0001 的算子只读这些字段——策略语言因此不需要理解整个协议。
    """

    mandate_id: str
    kind: Literal["open", "closed"] = "open"
    currency: str = "CNY"
    budget_total: float | None = None
    per_txn_max: float | None = None
    max_transactions: int | None = 1
    """一次 mandate 最多允许几次交易（ADR-0029）。demo 默认 1；None = 不限制。"""
    allowed_merchants: tuple[str, ...] = ()
    allowed_categories: tuple[str, ...] = ()
    expires_at: str | None = None
    binding: dict[str, Any] = field(default_factory=dict)
    """只有 closed mandate 才有：`checkout_ref` / `amount`（AP2 的 `transaction_id` 绑定）。"""


@dataclass(frozen=True)
class InterceptedRequest:
    """一次被拦截的方法调用（ADR-0011：所有能读数据或改变状态的方法）。"""

    method: str
    session_id: str
    """网关自建的会话标识（ADR-0017）——协议里没有会话概念，这是我们自己绑的。"""
    tool_call_id: str
    """网关签发的调用标识，**重试之间保持不变**——它是预算幂等键的一半（ADR-0002）。"""
    request_id: str | None = None
    name: str | None = None
    """工具名（`tools/call`）或资源 URI（`resources/read`）。"""
    arguments: dict[str, Any] = field(default_factory=dict)
    """**未校验**的原始参数——ADR-0014 说的「校验类」，策略从这里读金额与商户。"""
    meta: dict[str, Any] = field(default_factory=dict)
    now: float | None = None
    """求值时刻（POSIX 秒）。**由调用方传入**，策略求值因此仍是纯函数（architecture 第 4 条）：
    不传就不做过期判定，而不是在实现里偷读 `time.time()`。"""


@dataclass(frozen=True)
class AuthorizationBasis:
    """「谁、被授权做什么」——由 `identity/` 产出（ADR-0019 / 0020）。"""

    subject: str
    """调用方标识：来自下游 token，只用于识别调用方，**绝不转发给上游**（ADR-0019）。"""
    scopes: frozenset[str] = frozenset()
    """下游 token 的 scope。策略只能与它取交集，不能放宽（ADR-0020）。"""
    audience: str | None = None
    mandate: Mandate | None = None


@dataclass(frozen=True)
class Decision:
    """策略输出（architecture 第 3 条）。三种非 allow 形态都要在这里表达。"""

    kind: DecisionKind = "allow"
    reason_code: str | None = None
    hard: bool = False
    """True = 硬安全层：线上只给通用码，不回显阈值与规则名（ADR-0007 的三层方案）。"""
    approval_id: str | None = None
    patch: dict[str, Any] | None = None
    """`transform` 的改写内容（W3 可只留接口，实现留到需要时）。"""

    @property
    def allowed(self) -> bool:
        return self.kind == "allow"

