"""组装网关：上游 stdio 子进程 + 下游 Streamable HTTP。

生命周期：进入时连上游（客户端只在 `upstream.py` 里构造），退出时一起关掉；
证据账本跟着生命周期开合。
"""

from __future__ import annotations

from contextlib import AsyncExitStack
from typing import Any

from mcp.server.mcpserver import MCPServer
from starlette.applications import Starlette

from .config import GatewayConfig
from .enforce import Enforcer
from .evidence import EvidenceLog
from .frontdoor import build_server
from .upstream import UpstreamRegistry


class Gateway:
    """网关进程：`async with Gateway(config) as gateway:` 拿到它的 ASGI app 与账本。"""

    def __init__(self, config: GatewayConfig) -> None:
        self.config = config
        self.evidence = EvidenceLog(config.evidence_path, config.run_id)
        self.registry = UpstreamRegistry(config.upstreams)
        self.enforcer = Enforcer(self.registry, self.evidence)
        self.server: MCPServer | None = None
        self.app: Starlette | None = None
        self._stack: AsyncExitStack | None = None

    async def __aenter__(self) -> "Gateway":
        self._stack = AsyncExitStack()
        await self._stack.__aenter__()
        try:
            await self.registry.start(self._stack)
            self.server = build_server(self.enforcer)
            self.app = self.server.streamable_http_app(
                streamable_http_path=self.config.path, host=self.config.host
            )
        except BaseException:
            await self._stack.aclose()
            self.evidence.close()
            raise
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        if self._stack is not None:
            await self._stack.aclose()
        self.evidence.close()

    @property
    def url(self) -> str:
        return f"http://{self.config.host}:{self.config.port}{self.config.path}"

