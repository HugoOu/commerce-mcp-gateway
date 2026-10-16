"""前门：对 agent 暴露的 MCP server（Streamable HTTP，ADR-0004）——AI 起草，见 ADR-0026。

ADR-0005 的分工：这里只做「取身份 + 读客户端的协议能力 → 把请求交给执行点 → 返回结果」，
策略、预算与审批的**决策**逻辑一行都没有（包括「这个客户端能接受哪种审批通道」——这里只把
协议事实翻成一个通道名，怎么用它由执行点决定）。
工具清单与调用都由 middleware 短路提供，所以这里不注册任何工具；能力广告由 SDK 从已注册的
handler 推导（`tools/list`、`tools/call` 总是注册的），不受影响。
"""

from __future__ import annotations

from typing import Any

from mcp.server.context import ServerMiddleware
from mcp.server.mcpserver import MCPServer
from mcp.shared.exceptions import MCPError
from mcp_types import INVALID_PARAMS

from .approval import channel_for
from .enforce import Enforcer

INSTRUCTIONS = (
    "Commerce MCP Gateway：所有工具调用都会经过策略、预算与证据层。"
    "工具名以 `<上游>__` 前缀区分。"
)

PASSTHROUGH_METHODS = ("initialize", "server/discover", "ping")
"""交给 SDK 自己处理的方法（ADR-0011 排除的工具方法：读写不了业务数据）。

`initialize` 是**握手版**（2025-11-25）客户端的握手方法，`server/discover` 是现代版的对应物，
`ping` 是心跳。漏掉 `initialize` 的后果是实测出来的：老客户端连都连不上，直接
`MCPError: Not implemented: initialize`——而 ADR-0024 决定了网关要**同时服务两代**。
"""


def _bearer_token(ctx: Any) -> str | None:
    """从入站 HTTP 请求里取 bearer token（stdio 没有头，返回 None）。"""
    headers = getattr(getattr(ctx, "request", None), "headers", None)
    if not headers:
        return None
    value = headers.get("authorization") or ""
    if value.lower().startswith("bearer "):
        return value[7:].strip()
    return value or None


def _meta(ctx: Any) -> dict[str, Any] | None:
    """入站 `_meta`。实测（`docs/w3-plan.md` 的 MRTR 小节）：中间件看到的 `ctx.meta` 是
    **普通 dict**（不是模型），键形如 `io.modelcontextprotocol/protocolVersion`。"""
    meta = getattr(ctx, "meta", None)
    return meta if isinstance(meta, dict) else None


class GatewayMiddleware(ServerMiddleware[Any]):
    """拦截点（ADR-0005 推荐 1）：取身份 → 交给执行点 → 返回结果。

    它在 `ServerRunner` 的链路里跑在所有校验与查找之前，所以 `ctx.params` 是**未校验**的原始
    参数——正是 ADR-0014 说的「校验类」字段，策略从这里读金额与商户。

    入站消息按三类处理，界线是 ADR-0011 定的拦截面：

    1. **拦截**（`tools/*`、`resources/read`）→ 走执行点，每次都有 Decision；
    2. **放行**（`initialize` / `server/discover` / `ping`，以及**所有通知**）→ 原样交给 SDK。
       通知没有返回值、也读写不了业务数据（`classification.py` 里归 `PASSTHROUGH`），
       但 SDK 文档明说 `notifications/initialized` 也要在 `call_next` 里跑——不放行就等于
       把握手掐断；判别依据是 `ctx.request_id is None`（SDK 文档给的通知/请求判别式）；
    3. **其余一律结构化错误**（fail-closed）→ 包括 ADR-0013 里明确留作 future work 的
       `resources/list`。宁可报「不认识」也不静默放行：静默放行会让 I1
       （不存在未经 Decision 就放行的调用）随 SDK 版本悄悄失效。
    """

    def __init__(self, enforcer: Enforcer) -> None:
        self._enforcer = enforcer

    async def __call__(self, ctx: Any, call_next: Any) -> Any:
        method = ctx.method
        if ctx.request_id is None:
            return await call_next(ctx)  # 通知：没有返回值，也不在拦截面里
        if method in PASSTHROUGH_METHODS:
            return await call_next(ctx)  # 握手与心跳交给 SDK（ADR-0018：逐边协商、不翻译）

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
                # MRTR 的回答与 `_meta` **平级**（实测），所以从 `params` 取，不从 `ctx.meta` 取
                input_responses=params.get("inputResponses"),
                request_state=params.get("requestState"),
                # 通道是**协议事实**，不是策略判断：客户端广告了 elicitation 才能走 form（ADR-0021）
                approval_channel=channel_for(_meta(ctx)),
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

