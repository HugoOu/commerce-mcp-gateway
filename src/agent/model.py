"""模型客户端：一个协议 + 两个实现（真实调用 / 轨迹重放）。

为什么要有这层接口（ADR-0025）：不是为了支持多 provider，而是因为 ADR-0016 的
record&replay **必须能替换掉模型调用**——重放时不能再打真实 API。LiteLLM 之类的多
provider 适配器留作 future work，不进必装依赖。

环境变量（OpenAI 兼容端点）：
    OPENAI_API_KEY   API Key（真实调用时必填；纯重放不需要）
    OPENAI_BASE_URL  例如 https://opencode.ai/zen/go/v1
    MODEL_NAME       例如 deepseek-v4.1-flash
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class ToolCallSpec:
    """一次工具调用请求（与 OpenAI 的 tool_calls 一一对应）。"""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class Decision:
    """一次模型决策：给文本、给工具调用，或两者都给。"""

    content: str | None
    tool_calls: list[ToolCallSpec]
    raw: dict[str, Any] = field(default_factory=dict)
    """原样回填进 messages 的 assistant 消息（OpenAI 形状）。"""
    usage: dict[str, Any] = field(default_factory=dict)


class ModelClient(Protocol):
    """driver 只依赖这一个方法。"""

    async def complete(self, messages: list[dict], tools: list[dict]) -> Decision: ...


def _loads(raw: str | None) -> dict[str, Any]:
    try:
        parsed = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {"__raw_arguments": raw}
    return parsed if isinstance(parsed, dict) else {"__raw_arguments": raw}


class OpenAIModel:
    """默认实现：openai SDK 打 OpenAI 兼容端点。

    `openai` 是延迟导入的——纯重放路径不需要装它，也就不需要网络。
    """

    def __init__(self, model: str | None = None, api_key: str | None = None, base_url: str | None = None) -> None:
        from openai import AsyncOpenAI

        self.model = model or os.environ.get("MODEL_NAME", "deepseek-v4.1-flash")
        key = api_key or os.environ.get("OPENAI_API_KEY")
        if not key:
            raise RuntimeError("OPENAI_API_KEY is not set")
        self._client = AsyncOpenAI(api_key=key, base_url=base_url or os.environ.get("OPENAI_BASE_URL"))

    async def complete(self, messages: list[dict], tools: list[dict]) -> Decision:
        response = await self._client.chat.completions.create(
            model=self.model,
            messages=messages,
            tools=tools or None,
            tool_choice="auto" if tools else None,
        )
        message = response.choices[0].message
        calls = [
            ToolCallSpec(id=call.id, name=call.function.name, arguments=_loads(call.function.arguments))
            for call in (message.tool_calls or [])
        ]
        return Decision(
            content=message.content,
            tool_calls=calls,
            raw=message.model_dump(exclude_none=True),
            usage=response.usage.model_dump() if response.usage else {},
        )


class ReplayModel:
    """重放实现：按顺序吐出录下来的决策，不打任何 API、不读环境变量。"""

    def __init__(self, decisions: list[Decision]) -> None:
        self._decisions = list(decisions)
        self._cursor = 0

    async def complete(self, messages: list[dict], tools: list[dict]) -> Decision:
        if self._cursor >= len(self._decisions):
            raise RuntimeError("replay exhausted: 录制的轨迹里模型调用比这次少")
        decision = self._decisions[self._cursor]
        self._cursor += 1
        return decision


def decision_to_record(decision: Decision) -> dict[str, Any]:
    """把决策摊平成可 JSON 化的记录（轨迹文件里的一行）。"""
    return {
        "content": decision.content,
        "tool_calls": [
            {"id": call.id, "name": call.name, "arguments": call.arguments} for call in decision.tool_calls
        ],
        "raw": decision.raw,
        "usage": decision.usage,
    }


def decision_from_record(payload: dict[str, Any]) -> Decision:
    calls = [
        ToolCallSpec(id=call["id"], name=call["name"], arguments=call.get("arguments") or {})
        for call in payload.get("tool_calls", [])
    ]
    return Decision(
        content=payload.get("content"),
        tool_calls=calls,
        raw=payload.get("raw") or {},
        usage=payload.get("usage") or {},
    )
