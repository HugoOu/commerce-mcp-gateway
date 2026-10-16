"""手动玩网关的入口——W4 之前的自由体验，也是 `gateway` 命令行的雏形。

用法（项目根目录；`serve` 占一个窗口，其余命令在另一个窗口跑）：

    # 窗口 1：起网关（带策略与 mandate，默认 127.0.0.1:8765）
    uv run python scripts/play.py serve
    uv run python scripts/play.py serve --psp-mode decline --merchant-mode inject_in_messages

    # 窗口 2：随便玩
    uv run python scripts/play.py tools                  # 看被过滤后的工具清单
    uv run python scripts/play.py call psp__refund_payment payment_intent_id=pi_x
    uv run python scripts/play.py call psp__create_payment_intent amount=999 merchant_id=m1 checkout_ref=ck_x idempotency_key=k1
    uv run python scripts/play.py read mcp://m1/orders/secret
    uv run python scripts/play.py approvals / approve <id> / deny <id>
    uv run python scripts/play.py chain / verify [--run <run_id>]

    # 不用模型也不用手动敲：一条命令把整条链演一遍
    uv run python scripts/play.py scenario

    # 让真 agent 走一遍（需要 OPENAI_API_KEY）：用 scripts/run_w2_demo.py

参数写法：默认 `key=value`（值先按 JSON 解析，失败就当字符串），也接受一整段 JSON。
**PowerShell 会吃掉 JSON 里的双引号**（`'{"a": 1}'` 到 Python 手上变成 `{a: 1}`），
所以 JSON 写法在 PowerShell 里必须转义成 `'{\"a\": 1}'`——嫌麻烦就用 `key=value`。

`--host` / `--port` / `--merchant-mode` / `--psp-mode` 这些开关写在子命令**前面或后面都行**
（`play.py --psp-mode decline serve` 与 `play.py serve --psp-mode decline` 等价）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys
import time
from typing import Any

import uvicorn
from mcp.client.client import Client
from mcp.shared.exceptions import MCPError
from mcp_types import ElicitResult

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from gateway.app import Gateway
from gateway.approval import DurableApprovalGate
from gateway.config import GatewayConfig, UpstreamSpec
from gateway.evidence import ChainLedger

MOCKS = ROOT / "src" / "mcp_servers"
POLICY = ROOT / "src" / "gateway" / "policy" / "demo-policy.yaml"
MANDATE = ROOT / "src" / "gateway" / "demo-mandate.yaml"


def build_config(args: argparse.Namespace) -> GatewayConfig:
    return GatewayConfig(
        upstreams=[
            UpstreamSpec(
                alias="merchant",
                command=sys.executable,
                args=[str(MOCKS / "merchant.py")],
                env={"MOCK_MODE": args.merchant_mode, "MOCK_DELAY_S": "0.1"},
            ),
            UpstreamSpec(
                alias="psp",
                command=sys.executable,
                args=[str(MOCKS / "psp.py")],
                env={"PSP_MODE": args.psp_mode, "MOCK_DELAY_S": "0.1"},
            ),
        ],
        host=args.host,
        port=args.port,
        evidence_dir=pathlib.Path(args.evidence),
        budget_path=pathlib.Path(args.budget),
        approvals_path=pathlib.Path(args.approvals),
        policy_path=POLICY,
        mandate_path=MANDATE,
        approval_timeout_seconds=args.approval_timeout,
    )


def show(label: str, result: Any, *, limit: int = 1500) -> None:
    dump = result.model_dump(by_alias=True)
    texts = [block.get("text", "") for block in dump.get("content", []) if isinstance(block, dict)]
    print(f"{label}: isError={dump.get('isError')} resultType={dump.get('resultType')}")
    for text in texts:
        body = text.replace("\n", "\n  ")
        if len(body) > limit:
            body = f"{body[:limit]}\n  …（已截断，原文 {len(text)} 字符）"
        print("  " + body)


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


async def with_client(url: str, body) -> None:
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
            if "licitation" in str(exc.message):
                print("  （这个客户端没注册 elicitation 回调；网关的 CLI 兜底通道还没接通，见 w4-plan Step 1.2）")
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


def cmd_chain(args: argparse.Namespace) -> None:
    ledger = ChainLedger(pathlib.Path(args.evidence))
    path = (ledger.runs_dir / f"{args.run}.jsonl") if args.run else ledger.chain_path
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    print(f"{path}（{len(rows)} 条）")
    for row in rows:
        tool = row.get("tool") or ""
        extra = f" {tool}" if tool else ""
        print(f"  #{row['seq']:02d} {row['phase']:<7} {row['method']}{extra}"
              f" decision={row['decision']} outcome={row.get('outcome') or '-'}"
              f" reason={row.get('reason_code') or '-'}")


def cmd_verify(args: argparse.Namespace) -> None:
    ledger = ChainLedger(pathlib.Path(args.evidence))
    report = ledger.verify(args.run)
    print(f"verify(run={args.run}): ok={report.ok} entries={report.entries} broken_at={report.broken_at}")
    print("  " + report.detail)
    replay = ledger.replay(args.run)
    print(f"replay: 决策序列={replay.decisions} 前置条件满足={replay.preconditions_met}")


def cmd_approvals(args: argparse.Namespace) -> None:
    gate = DurableApprovalGate(pathlib.Path(args.approvals))
    pending = gate.pending()
    if not pending:
        print("没有待审批")
        return
    for item in pending:
        print(f"{item.approval_id}  {item.action}  subject={item.subject}  {item.reason_code}")


def cmd_decide(args: argparse.Namespace) -> None:
    path = pathlib.Path(args.approvals)
    row = {
        "kind": "decision",
        "approval_id": args.approval_id,
        "approved": args.action == "approve",
        "decided_at": round(time.time(), 3),
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"{args.approval_id} -> {'approved' if row['approved'] else 'denied'}")


async def approve_callback(*args: Any, **kwargs: Any) -> ElicitResult:
    return ElicitResult(action="accept", content={"approve": True})


async def cmd_scenario(args: argparse.Namespace) -> None:
    """不用模型：把正路与三条越权通道演一遍，并打印预算与证据。"""
    config = build_config(args)
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
            async with Client(server=gateway.url, mode="auto", elicitation_callback=approve_callback) as client:
                print(f"== 网关 {gateway.url}（run_id={config.run_id}）==")
                tools = [tool.name for tool in (await client.list_tools()).tools]
                print(f"\n1) 工具清单（{len(tools)} 个，ADR-0010 过滤后）：{tools}")
                show("\n2) 正路：建购物车", await client.call_tool("merchant__create_checkout", {"sku": "taco", "quantity": 1}))
                show("\n3) 正路：授权 9.71", await client.call_tool(
                    "psp__create_payment_intent",
                    {"amount": 9.71, "merchant_id": "m1", "checkout_ref": "ck_play", "idempotency_key": "k1"},
                ))
                show("\n4) 通道②：超上限 999", await client.call_tool(
                    "psp__create_payment_intent",
                    {"amount": 999.0, "merchant_id": "m1", "checkout_ref": "ck_play", "idempotency_key": "k2"},
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

    cmd_chain(argparse.Namespace(evidence=args.evidence, run=config.run_id))
    cmd_verify(argparse.Namespace(evidence=args.evidence, run=config.run_id))


async def cmd_serve(args: argparse.Namespace) -> None:
    config = build_config(args)
    async with Gateway(config) as gateway:
        assert gateway.app is not None
        print(f"[gateway] {gateway.url}（run_id={config.run_id}）——Ctrl+C 停止")
        server = uvicorn.Server(uvicorn.Config(gateway.app, host=config.host, port=config.port, log_level="warning"))
        await server.serve()


def _shared_flags() -> argparse.ArgumentParser:
    """让同一组开关在**子命令前后都能写**：`play.py --psp-mode decline serve` 与
    `play.py serve --psp-mode decline` 等价。

    子命令这一份的默认值必须是 `SUPPRESS`：argparse 的子解析器会把自己那份默认值写回
    namespace，把主解析器刚解析出来的值冲掉——这是 argparse 的经典坑，只加 `parents` 不够。
    """
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--host", default=argparse.SUPPRESS)
    shared.add_argument("--port", type=int, default=argparse.SUPPRESS)
    shared.add_argument("--evidence", default=argparse.SUPPRESS)
    shared.add_argument("--budget", default=argparse.SUPPRESS)
    shared.add_argument("--approvals", default=argparse.SUPPRESS)
    shared.add_argument("--merchant-mode", default=argparse.SUPPRESS)
    shared.add_argument("--psp-mode", default=argparse.SUPPRESS)
    shared.add_argument("--approval-timeout", type=float, default=argparse.SUPPRESS)
    return shared


def main() -> int:
    parser = argparse.ArgumentParser(description="手动玩 MCP 网关")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--evidence", default="runs/evidence")
    parser.add_argument("--budget", default="runs/budget.sqlite")
    parser.add_argument("--approvals", default="runs/approvals.jsonl")
    parser.add_argument("--merchant-mode", default="none")
    parser.add_argument("--psp-mode", default="none")
    parser.add_argument("--approval-timeout", type=float, default=30.0)
    shared = _shared_flags()
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve", parents=[shared])
    sub.add_parser("tools", parents=[shared])
    sub.add_parser("scenario", parents=[shared])
    call = sub.add_parser("call", parents=[shared])
    call.add_argument("tool")
    call.add_argument("args", nargs="*", default=[], help="key=value（推荐）或一整段 JSON")
    read = sub.add_parser("read", parents=[shared])
    read.add_argument("uri")
    chain = sub.add_parser("chain", parents=[shared])
    chain.add_argument("--run", default=None)
    verify = sub.add_parser("verify", parents=[shared])
    verify.add_argument("--run", default=None)
    sub.add_parser("approvals", parents=[shared])
    for name in ("approve", "deny"):
        decide = sub.add_parser(name, parents=[shared])
        decide.add_argument("approval_id")
    args = parser.parse_args()

    url = f"http://{args.host}:{args.port}/mcp"
    if args.cmd == "serve":
        asyncio.run(cmd_serve(args))
    elif args.cmd == "scenario":
        asyncio.run(cmd_scenario(args))
    elif args.cmd == "tools":
        asyncio.run(cmd_tools(url))
    elif args.cmd == "call":
        asyncio.run(cmd_call(url, args.tool, parse_arguments(args.args)))
    elif args.cmd == "read":
        asyncio.run(cmd_read(url, args.uri))
    elif args.cmd == "chain":
        cmd_chain(args)
    elif args.cmd == "verify":
        cmd_verify(args)
    elif args.cmd == "approvals":
        cmd_approvals(args)
    else:
        cmd_decide(args)
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

