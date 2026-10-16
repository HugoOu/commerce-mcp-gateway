"""网关命令行（`python -m gateway ...`）——AI 起草，见 ADR-0026。

三族子命令：

- **运行**：`serve`——默认起的是**演示配置**（mock merchant + mock PSP + demo 策略与 mandate）；
- **审计**（ADR-0003）：`chain` / `verify` / `replay`——只读账本，不碰网络；
- **审批投递**（ADR-0006 的 CLI 通道）：`approvals` / `approve` / `deny`。

调试用的**客户端**（`tools` / `call` / `read`）刻意不在这里：它们是开发工具，不是产品的一部分，
留在 `scripts/play.py`。这样 `src/gateway/` 里就不会出现 `mcp.client`——那是 I1 的结构契约
（`tests/test_layering.py`：只有 `upstream.py` 可以持有上游客户端）。

用法（项目根目录）：

    uv run python -m gateway serve
    uv run python -m gateway serve --psp-mode decline --merchant-mode inject_in_messages
    uv run python -m gateway chain / verify / replay [--run <run_id>]
    uv run python -m gateway approvals
    uv run python -m gateway approve <approval_id> / deny <approval_id>

开关写在子命令**前面或后面都行**。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys

import uvicorn

from .app import Gateway
from .approval import DurableApprovalGate
from .config import GatewayConfig, UpstreamSpec
from .evidence import ChainLedger

ROOT = pathlib.Path(__file__).resolve().parents[2]
"""项目根（`src/gateway/cli.py` 上溯两级）。演示配置里的 mock、策略与 mandate 都相对它定位——
所以这套默认值只对**源码检出**成立；装成包之后要显式给 `--policy` / `--mandate`。"""
MOCKS = ROOT / "src" / "mcp_servers"
DEMO_POLICY = ROOT / "src" / "gateway" / "policy" / "demo-policy.yaml"
DEMO_MANDATE = ROOT / "src" / "gateway" / "demo-mandate.yaml"

# 演示默认值只在这里定义一次：命令行与 `scripts/play.py` 都从这里取，免得两边各写一套
# 而在某次改动后悄悄分叉。
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_EVIDENCE = "runs/evidence"
DEFAULT_BUDGET = "runs/budget.sqlite"
DEFAULT_APPROVALS = "runs/approvals.jsonl"
DEFAULT_APPROVAL_TIMEOUT = 60.0


def demo_config(args: argparse.Namespace) -> GatewayConfig:
    """演示配置：两个 mock 上游 + demo 策略 + demo mandate。

    生产用法是给 `GatewayConfig` 传自己的上游与策略文件；这个函数只是把 W2 起就在用的那一套
    固定下来，免得每条命令都重复一遍路径。
    """
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
        policy_path=pathlib.Path(args.policy),
        mandate_path=pathlib.Path(args.mandate),
        approval_timeout_seconds=args.approval_timeout,
    )


# ---------------------------------------------------------------------- 运行


async def cmd_serve(args: argparse.Namespace) -> None:
    config = demo_config(args)
    async with Gateway(config) as gateway:
        assert gateway.app is not None
        print(f"[gateway] {gateway.url}（run_id={config.run_id}）——Ctrl+C 停止")
        server = uvicorn.Server(
            uvicorn.Config(gateway.app, host=config.host, port=config.port, log_level="warning")
        )
        await server.serve()


# ---------------------------------------------------------------------- 审计


def _scope(run: str | None) -> str:
    return f"run={run}" if run else "整条链"


def _rows(path: pathlib.Path) -> list[dict]:
    if not path.exists():
        return []
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def cmd_chain(args: argparse.Namespace) -> None:
    """把链（或某次运行）逐条打出来——这是「agent 到底做了什么」的唯一真相。"""
    ledger = ChainLedger(pathlib.Path(args.evidence))
    path = (ledger.runs_dir / f"{args.run}.jsonl") if args.run else ledger.chain_path
    rows = _rows(path)
    if not rows:
        print(f"没有记录：{path}")
        return
    print(f"{path}（{len(rows)} 条）")
    for row in rows:
        tool = row.get("tool") or ""
        suffix = f" {tool}" if tool else ""
        print(
            f"  #{row['seq']:02d} {row['phase']:<7} {row['method']}{suffix}"
            f" decision={row['decision']} outcome={row.get('outcome') or '-'}"
            f" reason={row.get('reason_code') or '-'}"
        )


def cmd_verify(args: argparse.Namespace) -> None:
    """校验链与运行证据包（ADR-0003）：单条篡改、包内截断、链被截断都要检出来。"""
    report = ChainLedger(pathlib.Path(args.evidence)).verify(args.run)
    print(
        f"verify（{_scope(args.run)}）：ok={report.ok} entries={report.entries}"
        f" broken_at={report.broken_at}"
    )
    print(f"  {report.detail}")


def cmd_replay(args: argparse.Namespace) -> None:
    """重放前置条件探针 + 记录里的决策序列（I4 的前身，见 `docs/evaluation.md`）。"""
    report = ChainLedger(pathlib.Path(args.evidence)).replay(args.run)
    print(
        f"replay（{_scope(args.run)}）：前置条件满足={report.preconditions_met} ok={report.ok}"
    )
    print(f"  决策序列：{list(report.decisions)}")
    if report.mismatches:
        print(f"  不一致位置：{list(report.mismatches)}")
    print("  注：链上落的是**输入摘要**（args_digest）而不是原文——这里能验「输入没变」，")
    print("      真正重算决策需要原文，见 docs/w4-plan.md Step 3 的待定口径。")


# ---------------------------------------------------------------------- 审批


def cmd_approvals(args: argparse.Namespace) -> None:
    gate = DurableApprovalGate(pathlib.Path(args.approvals))
    pending = gate.pending()
    if not pending:
        print("没有待审批")
        return
    for item in pending:
        print(f"{item.approval_id}  {item.action}  subject={item.subject}  {item.reason_code}")


def cmd_decide(args: argparse.Namespace) -> None:
    """投递一条决定（ADR-0006 的 CLI 通道）：网关那边在等的话，下一次轮询就会读到。"""
    approved = args.cmd == "approve"
    DurableApprovalGate(pathlib.Path(args.approvals)).decide(args.approval_id, approved=approved)
    print(f"{args.approval_id} -> {'approved' if approved else 'denied'}")


# ---------------------------------------------------------------------- 入口


def shared_flags() -> argparse.ArgumentParser:
    """让同一组开关在子命令**前后都能写**。

    子命令这一份的默认值必须是 `SUPPRESS`：argparse 的子解析器会把自己那份默认值写回
    namespace，把主解析器刚解析出来的值冲掉——这是 argparse 的经典坑，只加 `parents` 不够。
    """
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--host", default=argparse.SUPPRESS)
    shared.add_argument("--port", type=int, default=argparse.SUPPRESS)
    shared.add_argument("--evidence", default=argparse.SUPPRESS)
    shared.add_argument("--budget", default=argparse.SUPPRESS)
    shared.add_argument("--approvals", default=argparse.SUPPRESS)
    shared.add_argument("--policy", default=argparse.SUPPRESS)
    shared.add_argument("--mandate", default=argparse.SUPPRESS)
    shared.add_argument("--merchant-mode", default=argparse.SUPPRESS)
    shared.add_argument("--psp-mode", default=argparse.SUPPRESS)
    shared.add_argument("--approval-timeout", type=float, default=argparse.SUPPRESS)
    return shared


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gateway", description="commerce MCP gateway")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--evidence", default=DEFAULT_EVIDENCE, help="证据链目录")
    parser.add_argument("--budget", default=DEFAULT_BUDGET)
    parser.add_argument("--approvals", default=DEFAULT_APPROVALS)
    parser.add_argument("--policy", default=str(DEMO_POLICY))
    parser.add_argument("--mandate", default=str(DEMO_MANDATE))
    parser.add_argument("--merchant-mode", default="none", help="mock 商户的故障注入模式")
    parser.add_argument("--psp-mode", default="none", help="mock PSP 的故障注入模式")
    parser.add_argument("--approval-timeout", type=float, default=DEFAULT_APPROVAL_TIMEOUT,
                        help="等人工审批的秒数")
    shared = shared_flags()
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve", parents=[shared], help="起网关（默认演示配置）")
    chain = sub.add_parser("chain", parents=[shared], help="打印证据链")
    chain.add_argument("--run", default=None)
    verify = sub.add_parser("verify", parents=[shared], help="校验链与运行证据包")
    verify.add_argument("--run", default=None)
    replay = sub.add_parser("replay", parents=[shared], help="重放前置条件与决策序列")
    replay.add_argument("--run", default=None)
    sub.add_parser("approvals", parents=[shared], help="列出待审批")
    for name in ("approve", "deny"):
        decide = sub.add_parser(name, parents=[shared], help=f"{name} 一条待审批")
        decide.add_argument("approval_id")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "serve":
        asyncio.run(cmd_serve(args))
    elif args.cmd == "chain":
        cmd_chain(args)
    elif args.cmd == "verify":
        cmd_verify(args)
    elif args.cmd == "replay":
        cmd_replay(args)
    elif args.cmd == "approvals":
        cmd_approvals(args)
    else:
        cmd_decide(args)
    return 0

