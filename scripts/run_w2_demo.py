"""Step 3 验收：一条命令跑通「agent 经网关买一次东西」。

它同时起三样东西：两个 mock（由网关作为 stdio 子进程拉起）、网关（对 agent 暴露
Streamable HTTP）、driver（作为 MCP client 连网关）。跑完打印网关证据日志里的链路——
那一段就是「agent → 网关 → merchant → PSP → 回到 agent」的证据。

用法（项目根目录）：
    uv run python scripts/run_w2_demo.py
    uv run python scripts/run_w2_demo.py --merchant-mode inject_in_messages    # 给 Step 4 的调试用
    uv run python scripts/run_w2_demo.py --port 8790 --record runs/w2-demo.jsonl
"""

from __future__ import annotations

import argparse
import asyncio
import pathlib
import sys

import uvicorn
from mcp.client.client import Client

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent.driver import RunConfig, load_records, run_task
from gateway.app import Gateway
from gateway.config import GatewayConfig, UpstreamSpec

MOCKS = ROOT / "src" / "mcp_servers"


def build_config(args: argparse.Namespace) -> GatewayConfig:
    return GatewayConfig(
        upstreams=[
            UpstreamSpec(
                alias="merchant",
                command=sys.executable,
                args=[str(MOCKS / "merchant.py")],
                env={"MOCK_MODE": args.merchant_mode, "MOCK_DELAY_S": "0.2"},
            ),
            UpstreamSpec(
                alias="psp",
                command=sys.executable,
                args=[str(MOCKS / "psp.py")],
                env={"PSP_MODE": args.psp_mode, "MOCK_DELAY_S": "0.2"},
            ),
        ],
        host=args.host,
        port=args.port,
        evidence_dir=pathlib.Path(args.evidence),
        policy_path=pathlib.Path(args.policy) if args.policy else None,
        mandate_path=pathlib.Path(args.mandate) if args.mandate else None,
    )


async def wait_started(server: uvicorn.Server, timeout: float = 15.0) -> None:
    """等 uvicorn 真的开始监听；超时就直接失败，别让 driver 去猜。"""
    for _ in range(int(timeout * 20)):
        if server.started:
            return
        await asyncio.sleep(0.05)
    raise RuntimeError(f"网关在 {timeout}s 内没有起来")


async def print_tool_list(url: str) -> None:
    """把网关暴露的工具清单打出来——ADR-0010 的过滤效果要看得见。"""
    async with Client(server=url, mode="auto") as client:
        tools = [tool.name for tool in (await client.list_tools()).tools]
    print(f"[gateway] 暴露 {len(tools)} 个工具：{', '.join(tools)}")


def print_trace(path: pathlib.Path) -> None:
    print("\n--- 网关证据日志（agent → 网关 → 上游 → 回到 agent）---")
    for entry in load_records(path):
        tool = entry.get("tool") or ""
        suffix = f" {tool}" if tool else ""
        count = f" count={entry['count']}" if "count" in entry else ""
        duration = entry.get("duration_ms")
        timing = f" {duration}ms" if duration is not None else ""
        print(
            f"  #{entry['seq']:02d} {entry['method']}{suffix}"
            f" decision={entry.get('decision')} outcome={entry.get('outcome') or '-'}{count}{timing}"
        )


async def main_async(args: argparse.Namespace) -> int:
    config = build_config(args)
    async with Gateway(config) as gateway:
        print(f"[gateway] run_id={config.run_id} url={gateway.url} evidence={config.evidence_dir}")
        server = uvicorn.Server(
            uvicorn.Config(gateway.app, host=config.host, port=config.port, log_level="warning")
        )
        serving = asyncio.create_task(server.serve())
        await wait_started(server)
        await print_tool_list(gateway.url)
        try:
            driver_config = RunConfig(
                servers=[("gateway", gateway.url)],
                task=args.task,
                max_steps=args.max_steps,
                record_path=pathlib.Path(args.record) if args.record else None,
            )
            code = await run_task(driver_config)
        finally:
            server.should_exit = True
            await serving
    print_trace(config.evidence_dir / "chain.jsonl")
    return code


def main() -> int:
    parser = argparse.ArgumentParser(description="W2 Step 3 端到端 demo")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--task", default="买一份 taco")
    parser.add_argument("--max-steps", type=int, default=12)
    parser.add_argument("--merchant-mode", default="none")
    parser.add_argument("--psp-mode", default="none")
    parser.add_argument("--evidence", default="runs/evidence")
    parser.add_argument("--policy", default="src/gateway/policy/demo-policy.yaml")
    parser.add_argument("--mandate", default="src/gateway/demo-mandate.yaml")
    parser.add_argument("--record", default="runs/w2-demo.jsonl", help="driver 轨迹（空字符串表示不录）")
    args = parser.parse_args()
    return asyncio.run(main_async(args))


def leaves(exc: BaseException):
    if isinstance(exc, BaseExceptionGroup):
        for inner in exc.exceptions:
            yield from leaves(inner)
    else:
        yield exc


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001  顶层边界：把 ExceptionGroup 摊平
        for leaf in leaves(exc):
            print("[FATAL]", type(leaf).__name__, ":", leaf)
        raise SystemExit(1)

