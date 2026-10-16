"""策略文件的形状（ADR-0001）：YAML → pydantic → 内存决策结构。

写错字段要**立刻失败**（`extra="forbid"`）——策略文件静默忽略一个字段，等于静默放宽一条边界。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "ArgMatcher",
    "Binding",
    "Constraints",
    "Effect",
    "Match",
    "PolicyError",
    "PolicyFile",
    "Rule",
]

Effect = Literal["allow", "deny", "require_approval"]


class PolicyError(ValueError):
    """策略文件读不了 / 不合法。**加载即失败**，不做「尽量理解」。"""


class ArgMatcher(BaseModel):
    """参数条件的固定算子集（ADR-0001：不做通用 DSL、不做任意正则）。

    算子清单是封闭的：`eq` / `in` / `max` / `min` / `glob` / `exists`。
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    eq: Any | None = None
    in_: list[Any] | None = Field(default=None, alias="in")
    max: float | None = None
    min: float | None = None
    glob: str | None = None
    exists: bool | None = None

    def operators(self) -> list[str]:
        return [
            name
            for name in ("eq", "in_", "max", "min", "glob", "exists")
            if getattr(self, name) is not None
        ]


class Match(BaseModel):
    """命中条件：方法 + 工具名（段感知 glob）+ 资源 URI + 参数条件。"""

    model_config = ConfigDict(extra="forbid")

    method: str | None = None
    tool: str | None = None
    uri: str | None = None
    args: dict[str, ArgMatcher] = Field(default_factory=dict)


class Constraints(BaseModel):
    """约束（allow 时生效）。策略与 mandate 的约束**取交集**，永远只会更紧（ADR-0020）。"""

    model_config = ConfigDict(extra="forbid")

    amount_max: float | None = None
    merchant_in: list[str] = Field(default_factory=list)
    category_in: list[str] = Field(default_factory=list)


class Rule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    effect: Effect
    reason_code: str | None = None
    hard: bool = False
    """硬安全层：线上只给通用码（ADR-0007）。"""
    match: Match = Field(default_factory=Match)
    constraints: Constraints | None = None


class Binding(BaseModel):
    """把「参数 → 语义」显式声明出来，策略语言因此不需要理解每个工具的字段名。"""

    model_config = ConfigDict(extra="forbid")

    amount_field: str | None = None
    merchant_field: str | None = None
    category_field: str | None = None
    reserves_budget: bool = True
    """这次调用是否**占额度**（ADR-0027）。

    与 `amount_field` **刻意分开**：`amount_field` 说的是「金额在哪个参数里」（策略引擎据此做
    上限与白名单判定），`reserves_budget` 说的是「这次调用动不动钱」。

    为什么不能合成一个字段：授权（`create_payment_intent`）既有金额、又不动钱——如果靠「删掉
    `amount_field`」来表达「不占额度」，那么**上限判定也一起没了**（引擎只在声明了字段时才去读金额），
    999 元的授权会直接放行。这正是实现 ADR-0027 时被 `test_channel_two_over_limit_payment` 照出来的。
    """


class PolicyFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = 1
    default: Literal["deny"] = "deny"
    """只允许 deny——deny-by-default 是写死的，不给配置留后门。"""
    required_scopes: dict[str, str] = Field(
        default_factory=lambda: {"tools/call": "tools:call", "resources/read": "resources:read"}
    )
    bindings: dict[str, Binding] = Field(default_factory=dict)
    rules: list[Rule] = Field(default_factory=list)

    def model_post_init(self, _context: object, /) -> None:
        seen: set[str] = set()
        for rule in self.rules:
            if rule.id in seen:
                raise PolicyError(f"规则 id 重复：{rule.id}")
            seen.add(rule.id)
            if rule.effect != "allow" and not rule.reason_code:
                raise PolicyError(f"规则 {rule.id} 是 {rule.effect}，必须给 reason_code（ADR-0007）")
            for name, matcher in rule.match.args.items():
                if not matcher.operators():
                    raise PolicyError(f"规则 {rule.id} 的参数 {name} 没有任何算子")

