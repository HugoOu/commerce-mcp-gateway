"""上游客户端：**唯一**构造并持有 `mcp.client` 的地方（I1 的结构）。

ADR-0004：上游用 stdio 子进程；ADR-0005：转发自己写，不引入现成代理；
ADR-0008：工具名按 `<alias>__<tool>` 前缀，冲突 fail fast。

`tests/test_layering.py` 用 AST 把「只有本模块可以 import mcp.client」钉成 CI 契约——
这条契约就是 I1「不存在未经 Decision 就放行的调用」在代码结构上的那一半。
"""

from __future__ import annotations

from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any

import mcp_types as types
from mcp.client.client import Client
from mcp.client.stdio import StdioServerParameters

from .config import UpstreamSpec


class UnknownTool(LookupError):
    """名字不在聚合清单里——结构性错误，由执行点决定怎么回（ADR-0007 第 3 层）。"""


@dataclass
class Upstream:
    alias: str
    client: Client

    @property
    def prefix(self) -> str:
        return f"{self.alias}__"

    async def list_tools(self) -> list[types.Tool]:
        result = await self.client.list_tools()
        return [tool.model_copy(update={"name": f"{self.prefix}{tool.name}"}) for tool in result.tools]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        return await self.client.call_tool(name, arguments)


class UpstreamRegistry:
    """上游集合：启动、聚合工具清单、按前缀名路由。"""

    def __init__(self, specs: list[UpstreamSpec]) -> None:
        self._specs = specs
        self._upstreams: list[Upstream] = []
        self._routing: dict[str, tuple[Upstream, str]] = {}

    async def start(self, stack: AsyncExitStack) -> None:
        for spec in self._specs:
            params = StdioServerParameters(command=spec.command, args=spec.args, env=spec.env)
            client = await stack.enter_async_context(Client(server=params, mode="auto"))
            upstream = Upstream(alias=spec.alias, client=client)
            for tool in (await client.list_tools()).tools:
                qualified = f"{upstream.prefix}{tool.name}"
                if qualified in self._routing:
                    raise ValueError(f"工具名冲突（ADR-0008 fail fast）: {qualified}")
                self._routing[qualified] = (upstream, tool.name)
            self._upstreams.append(upstream)

    @property
    def aliases(self) -> list[str]:
        return [upstream.alias for upstream in self._upstreams]

    def resolve(self, qualified_name: str) -> tuple[Upstream, str]:
        try:
            return self._routing[qualified_name]
        except KeyError:
            raise UnknownTool(qualified_name) from None

    async def list_tools(self) -> list[types.Tool]:
        tools: list[types.Tool] = []
        for upstream in self._upstreams:
            tools.extend(await upstream.list_tools())
        return tools

