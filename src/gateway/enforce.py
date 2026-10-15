"""执行点（I1）：所有被拦截的方法调用都从这里过一遍。

W2 的行为是「全放行 + 记录」——策略求值、预算、审批都还没有（W3）。但结构上已经把三件事
固定下来：① 上游客户端只在这里被使用（且只在 `upstream.py` 里被构造）；② 每个请求先拿一个
`Decision`，再决定放行还是短路；③ 拒绝的形态按 ADR-0007 走，结构性错误才抛 `MCPError`。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Literal

import mcp_types as types
from mcp.shared.exceptions import MCPError
from mcp_types import INVALID_PARAMS

from . import telemetry
from .evidence import EvidenceLog, digest
from .upstream import UnknownTool, UpstreamRegistry


@dataclass(frozen=True)
class Decision:
    """W2 只有 allow；W3 长出 deny(reason_code) / require_approval / transform（architecture 第 3 条）。"""

    kind: Literal["allow", "deny"] = "allow"
    reason_code: str | None = None


class Enforcer:
    """唯一执行点。决策逻辑将来长在这里，而不是长在前门或 middleware 里（ADR-0005）。"""

    def __init__(self, registry: UpstreamRegistry, evidence: EvidenceLog) -> None:
        self._registry = registry
        self._evidence = evidence

    async def list_tools(self) -> types.ListToolsResult:
        """聚合上游工具清单（名字已按 ADR-0008 前缀）。W2 不做过滤与分页（ADR-0010/0013 属 W3）。"""
        started = time.perf_counter()
        tools = await self._registry.list_tools()
        duration_ms = (time.perf_counter() - started) * 1000
        self._evidence.record(
            method="tools/list",
            decision="allow",
            outcome="ok",
            count=len(tools),
            upstreams=self._registry.aliases,
            duration_ms=round(duration_ms, 3),
        )
        telemetry.annotate("tools/list", decision="allow", outcome="ok", duration_ms=duration_ms)
        return types.ListToolsResult(tools=tools)

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        """一次工具调用：resolve → Decision → 转发 → 记录。"""
        started = time.perf_counter()
        try:
            upstream, real_name = self._registry.resolve(name)
        except UnknownTool:
            # 找不到工具属于结构性错误：`CallToolResult` 的文档写得很直白——
            # 「Errors in finding the tool ... should be reported as an MCP error response」，
            # 与 ADR-0007 第 3 层（结构性 → 协议错误）一致。
            self._evidence.record(
                method="tools/call", tool=name, decision="error", outcome="unknown_tool", args_digest=digest(arguments)
            )
            raise MCPError(code=INVALID_PARAMS, message=f"Unknown tool: {name}", data=name) from None

        decision = Decision()  # W2：全放行。W3 在这里插入 policy → budget → approval（ADR-0007 第 2 层）。
        result = await upstream.call_tool(real_name, arguments)
        duration_ms = (time.perf_counter() - started) * 1000
        dumped = result.model_dump(by_alias=True)
        is_error = bool(dumped.get("isError"))
        self._evidence.record(
            method="tools/call",
            tool=name,
            upstream=upstream.alias,
            decision=decision.kind,
            outcome="tool_error" if is_error else "ok",
            args_digest=digest(arguments),
            result_digest=digest(dumped.get("content")),
            duration_ms=round(duration_ms, 3),
        )
        telemetry.annotate(
            "tools/call",
            tool=name,
            upstream=upstream.alias,
            decision=decision.kind,
            outcome="tool_error" if is_error else "ok",
            duration_ms=duration_ms,
        )
        return result

