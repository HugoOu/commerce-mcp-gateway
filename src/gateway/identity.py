"""身份与授权依据（ADR-0019 / 0020）——AI 起草，分工见 ADR-0026。

两条硬规矩：

1. 下游 token **只用于识别调用方**，绝不转发给上游（上游凭证由网关自持，ADR-0019）；
2. 策略**不能放宽**这里给出的 scope（ADR-0020 的 `scope ∩ policy`）。

演示与生产的区别只有一处开关：`allow_anonymous`。演示时没有 token 就按 `anonymous_subject`
处理；生产必须关掉它——那样缺 token、或 token 不认识，就是**硬拒绝**（`unauthenticated`），
由执行点按 ADR-0007 的硬安全层返回。
"""

from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass, field

import yaml

from .decision import AuthorizationBasis, Mandate

__all__ = ["IdentityConfig", "IdentityResolver", "TokenIdentity", "load_mandate"]


@dataclass(frozen=True)
class TokenIdentity:
    """一个 token 对应谁、被授予哪些 scope。"""

    subject: str
    scopes: tuple[str, ...] = ()


@dataclass(frozen=True)
class IdentityConfig:
    tokens: dict[str, TokenIdentity] = field(default_factory=dict)
    mandate_path: pathlib.Path | None = None
    audience: str = "commerce-mcp-gateway"
    """token 的 audience 必须绑定到本网关（ADR-0019 的 audience 绑定）。"""
    allow_anonymous: bool = True
    anonymous_subject: str = "agent-dev"
    anonymous_scopes: tuple[str, ...] = ("tools:call", "resources:read")


def load_mandate(path: pathlib.Path) -> Mandate:
    """读用户意图（YAML / JSON）。缺字段就是缺字段，不做默认值补全。"""
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) if path.suffix in (".yaml", ".yml") else json.loads(
        path.read_text(encoding="utf-8")
    )
    if not isinstance(raw, dict):
        raise ValueError(f"mandate 文件顶层必须是映射：{path}")
    if "allowed_merchants" in raw:
        raw["allowed_merchants"] = tuple(raw["allowed_merchants"])
    if "allowed_categories" in raw:
        raw["allowed_categories"] = tuple(raw["allowed_categories"])
    return Mandate(**raw)


class IdentityResolver:
    """token → `AuthorizationBasis`。认不出来就返回 `None`，由执行点硬拒。"""

    def __init__(self, config: IdentityConfig) -> None:
        self.config = config
        self.mandate = load_mandate(config.mandate_path) if config.mandate_path else None

    def basis(self, token: str | None) -> AuthorizationBasis | None:
        identity = self.config.tokens.get(token) if token else None
        if identity is None:
            if not self.config.allow_anonymous:
                return None
            identity = TokenIdentity(self.config.anonymous_subject, self.config.anonymous_scopes)
        return AuthorizationBasis(
            subject=identity.subject,
            scopes=frozenset(identity.scopes),
            audience=self.config.audience,
            mandate=self.mandate,
        )

