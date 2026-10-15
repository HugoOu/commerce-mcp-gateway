"""前门：对 agent 暴露的 MCP server（Streamable HTTP，ADR-0004）。

ADR-0005 的分工：这里只做「把请求交给执行点、把结果返回」，一行决策逻辑都没有。
工具清单与调用都由 middleware 短路提供，所以这里不注册任何工具；能力广告由 SDK 从已注册
的 handler 推导（`tools/list`、`tools/call` 总是注册的），不受影响。
"""

from __future__ import annotations

from typing import Any

from mcp.server.context import ServerMiddleware
from mcp.server.mcpserver import MCPServer
from mcp.shared.exceptions import MCPError
from mcp_types import INVALID_PARAMS

from .enforce import Enforcer

INSTRUCTIONS = (
    "Commerce MCP Gateway：所有工具调用都会经过策略、预算与证据层。"
    "工具名以 `<上游>__` 前缀区分；当前阶段只开放 tools/*。"
)


class GatewayMiddleware(ServerMiddleware[Any]):
    """拦截点（ADR-0005 推荐 1）：读原始 params → 交给 Enforcer → 返回结果。

    它在 `ServerRunner` 的链路里跑在所有校验与查找之前，所以拿到的 `ctx.params` 是**未校验**
    的原始参数——这正是 ADR-0014 说的「校验类」字段，W3 的策略会从这里读金额与商户。
    """

    def __init__(self, enforcer: Enforcer) -> None:
        self._enforcer = enforcer

    async def __call__(self, ctx: Any, call_next: Any) -> Any:
        method = ctx.method
        if method == "tools/list":
            return await self._enforcer.list_tools()
        if method == "tools/call":
            params = ctx.params or {}
            return await self._enforcer.call_tool(params.get("name") or "", dict(params.get("arguments") or {}))
        if method in ("server/discover", "ping"):
            # 版本探测与心跳交给 SDK（ADR-0018：逐边协商、不翻译）
            return await call_next(ctx)
        # W2 只实现 tools/*；其余面 fail-closed（结构性错误），见 w2-plan 的「实现分期」
        raise MCPError(code=INVALID_PARAMS, message=f"Not implemented in W2: {method}", data=method)


def build_server(enforcer: Enforcer) -> MCPServer:
    """构造下游 MCP server。拦截点通过构造函数注入，便于测试时替换。"""
    return MCPServer(
        name="commerce-mcp-gateway",
        version="0.1.0",
        instructions=INSTRUCTIONS,
        middleware=[GatewayMiddleware(enforcer)],
    )

