"""薄循环 agent driver（ADR-0016 / ADR-0025）：单步 action + 三类上限 + 轨迹落盘。

它做的事很少，但每一跳都能单独解释：
    system+task → 模型决策 →（有工具调用就）执行 MCP 工具 → 结果塞回 messages → 下一轮
停止条件：模型不再要工具（done）、步数上限、墙钟上限、token 上限。

轨迹：JSONL，每行一个事件（run_start / model_call / tool_call / run_end）。
重放：`--replay run.jsonl` 默认**连模型与 mock 都不用**（模型决策与工具结果都来自记录），
所以「模型不可用时重放仍能跑通」是天然成立的；加 `--live-tools` 可以只重放模型、真调工具。

用法见 `scripts/run_driver.py`。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import pathlib
import sys
import time
from collections import deque
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any

from mcp.client.client import Client
from mcp.client.stdio import StdioServerParameters

try:  # 既能当包导入，也能直接当脚本跑
    from .model import Decision, ModelClient, OpenAIModel, ReplayModel, decision_from_record, decision_to_record
except ImportError:  # pragma: no cover
    from model import Decision, ModelClient, OpenAIModel, ReplayModel, decision_from_record, decision_to_record

SYSTEM_PROMPT = """你是一个购物 agent，通过 MCP 工具完成一次购买。

任务：{task}

约束（mandate，必须遵守）：{mandate}

工作方式：
1. 先看目录（list_catalog），再建购物车（create_checkout），然后走支付（create_payment_intent → capture_payment），最后用 complete_checkout 完成订单。
2. 金额、商户、品类必须落在约束内；拿不准就停下来，用自然语言说明，不要硬试。
3. 工具报错时读错误信息，能修就修一次；修不了就停下来说明。
4. 完成或决定停下时，用一段自然语言总结（不要再调用工具）。"""


@dataclass
class RunConfig:
    servers: list[tuple[str, str]] = field(default_factory=list)
    task: str = "买一份 taco"
    mandate: str = "预算 100 CNY、单笔不超过 50 CNY、只允许商户 m1"
    model: str | None = None
    max_steps: int = 12
    max_seconds: float = 300.0
    max_tokens: int = 200_000
    record_path: pathlib.Path | None = None
    replay_path: pathlib.Path | None = None
    live_tools: bool = False


class RunRecorder:
    """JSONL 轨迹写入器：每条事件立刻 flush，崩溃也不丢前面的事件。"""

    def __init__(self, path: pathlib.Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._fh = path.open("a", encoding="utf-8")

    def write(self, **event: Any) -> None:
        self._fh.write(json.dumps({"ts": round(time.time(), 3), **event}, ensure_ascii=False) + "\n")
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()


def load_records(path: pathlib.Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def digest(events: list[dict[str, Any]]) -> str:
    """轨迹摘要：只看事件内容（不含时间戳），用来做「重放两次是否一致」的判据。"""
    blob = json.dumps(events, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


async def connect_servers(
    stack: AsyncExitStack, servers: list[tuple[str, str]]
) -> tuple[list[dict[str, Any]], dict[str, tuple[Client, str]]]:
    """连上若干 mock，把工具名按 ADR-0008 前缀成 `<alias>__<tool>`。"""
    tools: list[dict[str, Any]] = []
    routing: dict[str, tuple[Client, str]] = {}
    for alias, path in servers:
        client = await stack.enter_async_context(
            Client(server=StdioServerParameters(command=sys.executable, args=[path]), mode="auto")
        )
        for tool in (await client.list_tools()).tools:
            raw = tool.model_dump(by_alias=True)
            qualified = f"{alias}__{raw.get('name')}"
            routing[qualified] = (client, raw.get("name", ""))
            tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": qualified,
                        "description": raw.get("description") or "",
                        "parameters": raw.get("inputSchema") or {"type": "object", "properties": {}},
                    },
                }
            )
    return tools, routing


async def call_tool(client: Client, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    result = await client.call_tool(name, arguments)
    dump = result.model_dump(by_alias=True)
    texts = [block.get("text", "") for block in dump.get("content", []) if isinstance(block, dict)]
    return {"is_error": bool(dump.get("isError")), "text": " | ".join(texts)}


async def run_task(config: RunConfig) -> int:
    replay_records = load_records(config.replay_path) if config.replay_path else []
    start_event = next((r for r in replay_records if r.get("type") == "run_start"), {})
    pure_replay = bool(config.replay_path) and not config.live_tools

    if pure_replay:
        tools = start_event.get("tools") or []
        routing: dict[str, tuple[Client, str]] = {}
        decisions = [decision_from_record(r["response"]) for r in replay_records if r.get("type") == "model_call"]
        model: ModelClient = ReplayModel(decisions)
        recorded_tools: deque[dict[str, Any]] | None = deque(
            [r for r in replay_records if r.get("type") == "tool_call"]
        )
    else:
        model = OpenAIModel(model=config.model)
        recorded_tools = None

    recorder = RunRecorder(config.record_path) if config.record_path else None
    events: list[dict[str, Any]] = []
    started = time.monotonic()
    tokens = 0
    steps = 0
    status = "limit"

    async with AsyncExitStack() as stack:
        if not pure_replay:
            tools, routing = await connect_servers(stack, config.servers)

        if recorder:
            recorder.write(type="run_start", task=config.task, mandate=config.mandate,
                           model=config.model, max_steps=config.max_steps, tools=tools)

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT.format(task=config.task, mandate=config.mandate)},
        ]

        for step in range(config.max_steps):
            steps = step + 1
            decision = await model.complete(messages, tools)
            tokens += int((decision.usage or {}).get("total_tokens") or 0)
            response_record = decision_to_record(decision)
            # 摘要只覆盖「决策 + 结果」：messages 全文属于 request 侧，量大且可由前者推出
            events.append({"type": "model_call", "step": step, "response": response_record})
            if recorder:
                recorder.write(type="model_call", step=step,
                               request={"messages": messages, "tools": tools}, response=response_record)

            if not decision.tool_calls:
                status = "done"
                print(f"[done] {decision.content or ''}")
                break

            messages.append(decision.raw)
            for call in decision.tool_calls:
                if recorded_tools is not None:
                    recorded = recorded_tools.popleft() if recorded_tools else None
                    if recorded is None or recorded["name"] != call.name:
                        raise RuntimeError(f"replay mismatch: 记录里下一个工具是 {recorded and recorded['name']}，这次要调 {call.name}")
                    result = recorded["result"]
                else:
                    client, real_name = routing[call.name]
                    result = await call_tool(client, real_name, call.arguments)
                tool_event = {"type": "tool_call", "step": step, "name": call.name,
                              "arguments": call.arguments, "result": result}
                events.append(tool_event)
                if recorder:
                    recorder.write(**tool_event)
                messages.append({"role": "tool", "tool_call_id": call.id, "content": result["text"]})
                print(f"[tool] {call.name}({json.dumps(call.arguments, ensure_ascii=False)}) -> {result['text'][:90]}")

            if time.monotonic() - started > config.max_seconds:
                status = "timeout"
                print("[stop] 超过墙钟上限")
                break
            if tokens > config.max_tokens:
                status = "token_limit"
                print("[stop] 超过 token 上限")
                break
        else:
            status = "step_limit"
            print("[stop] 超过步数上限")

    if recorder:
        recorder.write(type="run_end", status=status, steps=steps, tokens=tokens, digest=digest(events))
        recorder.close()

    print(f"\nstatus={status} steps={steps} tokens={tokens} digest={digest(events)}")
    if config.record_path:
        print(f"record={config.record_path}")
    if pure_replay:
        print("replay=1（未使用模型、未连接 mock）")
    return 0 if status == "done" else 1


def parse_servers(values: list[str]) -> list[tuple[str, str]]:
    servers = []
    for value in values:
        alias, _, path = value.partition("=")
        if not path:
            raise SystemExit(f"--server 需要 alias=path 形式，收到 {value!r}")
        servers.append((alias, path))
    return servers


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="薄循环 driver（ADR-0016 / ADR-0025）")
    parser.add_argument("--server", action="append", default=[], help="alias=path，可重复")
    parser.add_argument("--task", default="买一份 taco")
    parser.add_argument("--mandate", default="预算 100 CNY、单笔不超过 50 CNY、只允许商户 m1")
    parser.add_argument("--model", default=None)
    parser.add_argument("--max-steps", type=int, default=12)
    parser.add_argument("--max-seconds", type=float, default=300.0)
    parser.add_argument("--max-tokens", type=int, default=200_000)
    parser.add_argument("--record", type=pathlib.Path, default=None, help="把轨迹写成 JSONL")
    parser.add_argument("--replay", type=pathlib.Path, default=None, help="重放一条轨迹")
    parser.add_argument("--live-tools", action="store_true", help="重放模型、真调工具")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = RunConfig(
        servers=parse_servers(args.server),
        task=args.task,
        mandate=args.mandate,
        model=args.model,
        max_steps=args.max_steps,
        max_seconds=args.max_seconds,
        max_tokens=args.max_tokens,
        record_path=args.record,
        replay_path=args.replay,
        live_tools=args.live_tools,
    )
    if not config.servers and not config.replay_path:
        raise SystemExit("要么给 --server，要么给 --replay")
    return asyncio.run(run_task(config))


if __name__ == "__main__":
    raise SystemExit(main())
