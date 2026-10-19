"""调试用客户端（`python scripts/play.py ...`）——**不是产品的一部分**。

产品侧的入口是 `python -m gateway`（`serve` / `chain` / `verify` / `replay` / `approvals` /
`approve` / `deny`）。这里只留三件调试用的事：看工具清单、手动调一次工具、读一个资源；再加
一条把整条链演一遍的 `scenario`（W4 Step 5 的三分钟演示从它来）。

为什么单独放 `scripts/`？因为这几条命令要**当 MCP 客户端**，而 `src/gateway/` 里只允许
`upstream.py` 持有 `mcp.client`（I1 的结构契约，`tests/test_layering.py`）。

用法（项目根目录；`serve` 占一个窗口）：

    uv run python -m gateway serve
    uv run python scripts/play.py tools
    uv run python scripts/play.py call merchant__create_checkout sku=taco quantity=1
    uv run python scripts/play.py call psp__create_payment_intent amount=999 merchant_id=m1 checkout_ref=ck_x idempotency_key=k1
    uv run python scripts/play.py read mcp://m1/orders/secret
    uv run python scripts/play.py scenario
    uv run python scripts/run_w2_demo.py        # 让真 agent 走一遍（需要模型 key）

参数写法：默认 `key=value`（值先按 JSON 解析，失败就当字符串），也接受一整段 JSON。
**PowerShell 会吃掉 JSON 里的双引号**（`'{"a": 1}'` 到 Python 手上变成 `{a: 1}`），
所以 JSON 写法在 PowerShell 里必须转义成 `'{\"a\": 1}'`——嫌麻烦就用 `key=value`。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys
from typing import Any

import uvicorn
from mcp.client.client import Client
from mcp.shared.exceptions import MCPError
from mcp_types import ElicitResult

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from gateway import cli
from gateway.app import Gateway


def show(label: str, result: Any, *, limit: int = 1500) -> Any:
    dump = result.model_dump(by_alias=True)
    texts = [block.get("text", "") for block in dump.get("content", []) if isinstance(block, dict)]
    print(f"{label}: isError={dump.get('isError')} resultType={dump.get('resultType')}")
    for text in texts:
        body = text.replace("\n", "\n  ")
        if len(body) > limit:
            body = f"{body[:limit]}\n  …（已截断，原文 {len(text)} 字符）"
        print("  " + body)
    return result


def parse_arguments(parts: list[str]) -> dict[str, Any]:
    """把命令行给的参数变成 dict——两种写法，都是为了绕开 shell 的引号处理。

    - `key=value`（推荐）：`call merchant__create_checkout sku=taco quantity=1`；
    - 一整段 JSON：`call ... '{"sku": "taco"}'`（**PowerShell 里必须转义双引号**）。

    值先按 JSON 解析（`1` → int、`true` → bool），失败就当字符串——所以 `sku=taco` 不用加引号。
    """
    if not parts:
        return {}
    joined = " ".join(parts)
    if joined.lstrip().startswith("{"):
        try:
            parsed = json.loads(joined)
        except json.JSONDecodeError as exc:
            raise SystemExit(
                f"参数不是合法 JSON（{exc.msg} @ 位置 {exc.pos}）：{joined}\n"
                "  PowerShell 会把 JSON 里的双引号吃掉，到 Python 手上就变成 {sku: taco} 这种形状。\n"
                "  两个办法：① 用 key=value 写法（推荐）；② 写成 '{\\\"sku\\\": \\\"taco\\\"}' 转义。"
            ) from None
        if not isinstance(parsed, dict):
            raise SystemExit(f"参数必须是一个 JSON 对象：{joined}")
        return parsed
    arguments: dict[str, Any] = {}
    for part in parts:
        key, separator, value = part.partition("=")
        if not separator:
            raise SystemExit(f"参数要写成 key=value（或用一整段 JSON），收到 {part!r}")
        try:
            arguments[key] = json.loads(value)
        except json.JSONDecodeError:
            arguments[key] = value
    return arguments


async def with_client(url: str, body: Any) -> None:
    async with Client(server=url, mode="auto") as client:
        await body(client)


async def cmd_tools(url: str) -> None:
    async def body(client: Client) -> None:
        tools = (await client.list_tools()).tools
        print(f"网关暴露 {len(tools)} 个工具：")
        for tool in tools:
            print(f"  - {tool.name}")
        print("（上游一共 14 个；被策略**直接拒绝**的 6 个不会出现在这里——ADR-0010）")

    await with_client(url, body)


async def cmd_call(url: str, tool: str, arguments: dict[str, Any]) -> None:
    async def body(client: Client) -> None:
        try:
            result = await client.call_tool(tool, arguments)
        except MCPError as exc:
            print(f"call {tool}: MCPError code={exc.code} message={exc.message!r}")
            return
        show(f"call {tool}", result)

    await with_client(url, body)


async def cmd_read(url: str, uri: str) -> None:
    async def body(client: Client) -> None:
        try:
            await client.session.read_resource(uri)
            print("意外：读到了资源")
        except MCPError as exc:
            print(f"read {uri}: MCPError code={exc.code} message={exc.message!r}（越界与不存在同形）")

    await with_client(url, body)


async def approve_callback(*args: Any, **kwargs: Any) -> ElicitResult:
    """`scenario` 专用：自动批准，好让整条链一路演下去。"""
    return ElicitResult(action="accept", content={"approve": True})


def scenario_args(args: argparse.Namespace) -> argparse.Namespace:
    """`scenario` 的配置：先取 CLI 的演示默认值，再盖上 play.py 自己解析出来的开关。

    这样演示配置的默认值只有一处定义（`gateway.cli`），play.py 不会另写一套而在某次改动后
    悄悄分叉。
    """
    merged = cli.build_parser().parse_args(["serve"])
    for key in (
        "host",
        "port",
        "evidence",
        "budget",
        "approvals",
        "policy",
        "mandate",
        "merchant_mode",
        "psp_mode",
        "approval_timeout",
    ):
        if hasattr(args, key):
            setattr(merged, key, getattr(args, key))
    return merged


async def cmd_scenario(args: argparse.Namespace) -> None:
    """不用模型：把正路与三条越权通道演一遍，并打印预算与证据。"""
    config = cli.demo_config(scenario_args(args))
    async with Gateway(config) as gateway:
        assert gateway.app is not None
        server = uvicorn.Server(
            uvicorn.Config(gateway.app, host=config.host, port=config.port, log_level="warning")
        )
        serving = asyncio.create_task(server.serve())
        for _ in range(200):
            if server.started:
                break
            await asyncio.sleep(0.05)
        try:
            async with Client(
                server=gateway.url, mode="auto", elicitation_callback=approve_callback
            ) as client:
                print(f"== 网关 {gateway.url}（run_id={config.run_id}）==")
                tools = [tool.name for tool in (await client.list_tools()).tools]
                print(f"\n1) 工具清单（{len(tools)} 个，ADR-0010 过滤后）：{tools}")
                checkout_result = await client.call_tool(
                    "merchant__create_checkout", {"sku": "taco", "quantity": 1}
                )
                show("\n2) 正路：建购物车", checkout_result)
                checkout = json.loads(checkout_result.content[0].text)
                checkout_id = checkout["checkout_id"]
                total = checkout["totals"]["total"]
                show(f"\n3) 正路：授权 {total}", await client.call_tool(
                    "psp__create_payment_intent",
                    {
                        "amount": total,
                        "merchant_id": "m1",
                        "checkout_ref": checkout_id,
                        "idempotency_key": "k1",
                    },
                ))
                show("\n4) 通道②：超上限 999", await client.call_tool(
                    "psp__create_payment_intent",
                    {
                        "amount": 999.0,
                        "merchant_id": "m1",
                        "checkout_ref": checkout_id,
                        "idempotency_key": "k2",
                    },
                ))
                show("\n5) 通道①：白名单外的工具", await client.call_tool(
                    "psp__refund_payment", {"payment_intent_id": "pi_x"}
                ))
                show("\n6) 审批：退款要走人工（这里由 elicitation 自动批准）", await client.call_tool(
                    "merchant__request_refund", {"order_id": "ord_missing", "reason": "play"}
                ))
                print("\n7) 通道③：越界 URI 读取")
                await cmd_read(gateway.url, "mcp://m1/orders/secret")
                balance = gateway.budget.balance("agent-dev")
                print(f"\n8) 预算：committed={balance.committed} active={balance.active_holds}"
                      f" remaining={balance.remaining}（被拒的调用不占额度）")
        finally:
            server.should_exit = True
            await serving

    scope = argparse.Namespace(evidence=str(config.evidence_dir), run=config.run_id)
    cli.cmd_chain(scope)
    cli.cmd_verify(scope)


def build_parser() -> argparse.ArgumentParser:
    """play.py 自己的命令行：只有客户端命令与 `scenario`（其余都归 `python -m gateway`）。"""
    parser = argparse.ArgumentParser(prog="play.py", description="调试用 MCP 客户端")
    parser.add_argument("--host", default=cli.DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=cli.DEFAULT_PORT)
    parser.add_argument("--evidence", default=cli.DEFAULT_EVIDENCE)
    parser.add_argument("--budget", default=cli.DEFAULT_BUDGET)
    parser.add_argument("--approvals", default=cli.DEFAULT_APPROVALS)
    parser.add_argument("--merchant-mode", default="none")
    parser.add_argument("--psp-mode", default="none")
    parser.add_argument("--approval-timeout", type=float, default=cli.DEFAULT_APPROVAL_TIMEOUT)
    shared = cli.shared_flags()
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("tools", parents=[shared])
    sub.add_parser("scenario", parents=[shared])
    call = sub.add_parser("call", parents=[shared])
    call.add_argument("tool")
    call.add_argument("args", nargs="*", default=[], help="key=value（推荐）或一整段 JSON")
    read = sub.add_parser("read", parents=[shared])
    read.add_argument("uri")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    url = f"http://{args.host}:{args.port}/mcp"
    if args.cmd == "scenario":
        asyncio.run(cmd_scenario(args))
    elif args.cmd == "tools":
        asyncio.run(cmd_tools(url))
    elif args.cmd == "call":
        asyncio.run(cmd_call(url, args.tool, parse_arguments(args.args)))
    else:
        asyncio.run(cmd_read(url, args.uri))
    return 0


def leaves(exc: BaseException):
    """把 anyio / asyncio 的 ExceptionGroup 摊平——否则一个参数写错会打出一屏 traceback。"""
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
    except BaseException as exc:  # noqa: BLE001  顶层边界：只打印叶子错误
        for leaf in leaves(exc):
            print("[FATAL]", type(leaf).__name__, ":", leaf)
        raise SystemExit(1)

