"""组装网关：上游 stdio 子进程 + 下游 Streamable HTTP + 控制面内核——AI 起草，见 ADR-0026。

生命周期：进入时连上游、跑一次启动对账，退出时一起关掉。
"""

from __future__ import annotations

import time
from contextlib import AsyncExitStack
from typing import Self

from mcp.server.mcpserver import MCPServer
from starlette.applications import Starlette

from .approval import DurableApprovalGate
from .budget import SqliteBudgetLedger
from .config import GatewayConfig
from .enforce import Enforcer
from .evidence import ChainLedger
from .frontdoor import build_server
from .identity import IdentityResolver, load_mandate
from .policy import AllowAllPolicy, load_policy
from .sanitize import Sanitizer
from .upstream import UpstreamRegistry


class Gateway:
    """网关进程：`async with Gateway(config) as gateway:` 拿到它的 ASGI app 与账本。"""

    def __init__(self, config: GatewayConfig) -> None:
        self.config = config
        self.evidence = ChainLedger(config.evidence_dir)
        self.budget = SqliteBudgetLedger(config.budget_path)
        self.policy = load_policy(config.policy_path) if config.policy_path else AllowAllPolicy()
        self.identity = IdentityResolver(
            config.identity,
            mandate=load_mandate(config.mandate_path) if config.mandate_path else None,
        )
        self.sanitizer = Sanitizer(enabled=config.sanitize, redact=config.sanitize_redact)
        self.approval = DurableApprovalGate(config.approvals_path, timeout_seconds=config.approval_timeout_seconds)
        self.registry = UpstreamRegistry(config.upstreams)
        self.enforcer = Enforcer(
            registry=self.registry,
            evidence=self.evidence,
            policy=self.policy,
            budget=self.budget,
            identity=self.identity,
            sanitizer=self.sanitizer,
            approval=self.approval,
            run_id=config.run_id,
            list_ttl_ms=config.list_ttl_ms,
        )
        self.server: MCPServer | None = None
        self.app: Starlette | None = None
        self._stack: AsyncExitStack | None = None

    async def __aenter__(self) -> Self:
        self._stack = AsyncExitStack()
        await self._stack.__aenter__()
        try:
            if self.config.policy_path is None:
                print("[gateway] 警告：没有给策略文件，本次**全放行**（只该用于演示）")
            reclaimed = self.budget.reclaim_expired(now=time.time())  # 启动对账（ADR-0002）
            if reclaimed:
                print(f"[gateway] 启动对账：回收了 {len(reclaimed)} 条过期 hold")
            await self.registry.start(self._stack)
            self.server = build_server(self.enforcer)
            self.app = self.server.streamable_http_app(
                streamable_http_path=self.config.path, host=self.config.host
            )
        except BaseException:
            await self._stack.aclose()
            self.close()
            raise
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self._stack is not None:
            await self._stack.aclose()
        self.close()

    def close(self) -> None:
        self.budget.close()

    @property
    def url(self) -> str:
        return f"http://{self.config.host}:{self.config.port}{self.config.path}"

