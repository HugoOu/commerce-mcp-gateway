"""前门：对 agent 暴露的 MCP server（Streamable HTTP，ADR-0004）——AI 起草，见 ADR-0026。

ADR-0005 的分工：这里只做「取身份 → 把请求交给执行点 → 返回结果」，一行决策逻辑都没有。
工具清单与调用都由 middleware 短路提供，所以这里不注册任何工具；能力广告由 SDK 从已注册的
handler 推导（`tools/list`、`tools/call` 总是注册的），不受影响。
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
    "工具名以 `<上游>__` 前缀区分。"
)


def _bearer_token(ctx: Any) -> str | None:
    """从入站 HTTP 请求里取 bearer token（stdio 没有头，返回 None）。"""
    headers = getattr(getattr(ctx, "request", None), "headers", None)
    if not headers:
        return None
    value = headers.get("authorization") or ""
    if value.lower().startswith("bearer "):
        return value[7:].strip()
    return value or None


class GatewayMiddleware(ServerMiddleware[Any]):
    """拦截点（ADR-0005 推荐 1）：取身份 → 交给执行点 → 返回结果。

    它在 `ServerRunner` 的链路里跑在所有校验与查找之前，所以 `ctx.params` 是**未校验**的原始
    参数——正是 ADR-0014 说的「校验类」字段，策略从这里读金额与商户。
    """

    def __init__(self, enforcer: Enforcer) -> None:
        self._enforcer = enforcer

    async def __call__(self, ctx: Any, call_next: Any) -> Any:
        method = ctx.method
        if method in ("server/discover", "ping"):
            return await call_next(ctx)  # 版本探测与心跳交给 SDK（ADR-0018：逐边协商、不翻译）

        basis = self._enforcer.basis(_bearer_token(ctx))
        if basis is None:
            self._enforcer.unauthenticated()

        if method == "tools/list":
            return await self._enforcer.list_tools(basis)
        if method == "tools/call":
            params = ctx.params or {}
            return await self._enforcer.call_tool(
                str(params.get("name") or ""),
                dict(params.get("arguments") or {}),
                basis=basis,
                request_id=ctx.request_id,
            )
        if method == "resources/read":
            params = ctx.params or {}
            return await self._enforcer.read_resource(
                str(params.get("uri") or ""), basis=basis, request_id=ctx.request_id
            )
        raise MCPError(code=INVALID_PARAMS, message=f"Not implemented: {method}", data=method)


def build_server(enforcer: Enforcer) -> MCPServer:
    """构造下游 MCP server。拦截点通过构造函数注入，便于测试时替换。"""
    return MCPServer(
        name="commerce-mcp-gateway",
        version="0.2.0",
        instructions=INSTRUCTIONS,
        middleware=[GatewayMiddleware(enforcer)],
    )

