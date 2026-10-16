"""审批的**通道选择**（W4 Step 1.2 / ADR-0006）——协议通道与 CLI 兜底通道。

这个文件专测「通道怎么选、选错了会怎样」；越权通道被拦在 `test_gateway_enforcement.py`。
两处的 harness 长得像，但都不到 40 行，等第三处出现时再抽 `conftest.py`（rule of three）。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import pathlib
import socket
import sys
import time
from collections.abc import AsyncIterator

import mcp_types as types
import pytest
import uvicorn
from mcp.client.client import Client
from mcp.shared.exceptions import MCPError

from gateway.app import Gateway
from gateway.approval import DurableApprovalGate, channel_for
from gateway.config import GatewayConfig, UpstreamSpec
from gateway.decision import AuthorizationBasis, Decision, InterceptedRequest

ROOT = pathlib.Path(__file__).resolve().parents[1]
MOCKS = ROOT / "src" / "mcp_servers"
POLICY = ROOT / "src" / "gateway" / "policy" / "demo-policy.yaml"
MANDATE = ROOT / "src" / "gateway" / "demo-mandate.yaml"

REFUND = "merchant__request_refund"


# ---------------------------------------------------------------------- 纯函数：通道怎么选


def test_channel_for_is_fail_closed() -> None:
    """通道是**协议事实**推出来的，两处都 fail-closed。

    - 没有 `_meta`（或没有版本号）→ `none`：不知道对方能不能读 `input_required`，就当不能；
    - 老客户端 → `none`：它读不懂现代版的 `input_required` 结果；
    - 现代版但没广告 elicitation → `cli`（兜底通道），不是放弃审批；
    - 现代版且广告了 elicitation → `form`。
    """
    assert channel_for(None) == "none"
    assert channel_for({}) == "none"
    assert channel_for({"io.modelcontextprotocol/protocolVersion": "2025-11-25"}) == "none"
    assert channel_for({"io.modelcontextprotocol/protocolVersion": "2026-07-28"}) == "cli"
    assert (
        channel_for(
            {
                "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                "io.modelcontextprotocol/clientCapabilities": {"elicitation": {"form": {}}},
            }
        )
        == "form"
    )


def test_channel_for_ignores_elicitation_on_legacy() -> None:
    """老客户端**即使**广告了 elicitation 也不能走 form：我们要发的是现代版的结果形状。"""
    assert (
        channel_for(
            {
                "io.modelcontextprotocol/protocolVersion": "2025-11-25",
                "io.modelcontextprotocol/clientCapabilities": {"elicitation": {"form": {}}},
            }
        )
        == "none"
    )


# ---------------------------------------------------------------------- 闸门本身


def _request(arguments: dict) -> InterceptedRequest:
    return InterceptedRequest(
        method="tools/call",
        session_id="agent-dev",
        tool_call_id="t1",
        name=REFUND,
        arguments=arguments,
        now=0.0,
    )


def _basis(subject: str = "agent-dev") -> AuthorizationBasis:
    return AuthorizationBasis(subject=subject, scopes=frozenset({"tools:call"}))


def test_register_is_idempotent_while_undecided(tmp_path: pathlib.Path) -> None:
    """同一（动作指纹, 主体）在未决定期间只有一条——否则客户端每重试一轮就刷一条。"""
    gate = DurableApprovalGate(tmp_path / "approvals.jsonl")
    decision = Decision(kind="require_approval", reason_code="refund_requires_approval")
    first = gate.register(_request({"order_id": "ord_1"}), decision, _basis())
    again = gate.register(_request({"order_id": "ord_1"}), decision, _basis())
    assert first.approval_id == again.approval_id
    assert len(gate.pending()) == 1

    # 参数变了就是另一个动作 → 另一条待审批
    other = gate.register(_request({"order_id": "ord_2"}), decision, _basis())
    assert other.approval_id != first.approval_id
    assert len(gate.pending()) == 2


def test_register_does_not_reuse_a_decided_approval(tmp_path: pathlib.Path) -> None:
    """批准过的不复用（ADR-0021 的「单次使用」）——否则一次批准会变成永久通行证。"""
    gate = DurableApprovalGate(tmp_path / "approvals.jsonl")
    decision = Decision(kind="require_approval", reason_code="refund_requires_approval")
    first = gate.register(_request({"order_id": "ord_1"}), decision, _basis())
    gate.decide(first.approval_id, approved=True)
    second = gate.register(_request({"order_id": "ord_1"}), decision, _basis())
    assert second.approval_id != first.approval_id


def test_resume_rejects_a_forged_request_state(tmp_path: pathlib.Path) -> None:
    """`request_state` 是客户端给的——动作指纹或主体对不上就不能认。"""
    gate = DurableApprovalGate(tmp_path / "approvals.jsonl")
    decision = Decision(kind="require_approval", reason_code="refund_requires_approval")
    pending = gate.register(_request({"order_id": "ord_1"}), decision, _basis())

    # 自己的动作 + 自己的主体 → 认
    assert gate.resume(pending.approval_id, _request({"order_id": "ord_1"}), _basis()) is not None
    # 换一个动作 → 不认（拿别人的批准给自己的调用开路）
    assert gate.resume(pending.approval_id, _request({"order_id": "ord_9"}), _basis()) is None
    # 换一个主体 → 不认（ADR-0021 要求绑定到具体的 client 与 user）
    assert gate.resume(pending.approval_id, _request({"order_id": "ord_1"}), _basis("someone-else")) is None
    # 不存在的句柄 → 不认
    assert gate.resume("apr_nope", _request({"order_id": "ord_1"}), _basis()) is None


def test_wait_budget_is_counted_from_registration(tmp_path: pathlib.Path) -> None:
    """等待预算从**登记时刻**算起，所以客户端多试几轮也续不了命。"""
    gate = DurableApprovalGate(tmp_path / "approvals.jsonl", timeout_seconds=10.0)
    decision = Decision(kind="require_approval", reason_code="refund_requires_approval")
    pending = gate.register(_request({"order_id": "ord_1"}), decision, _basis())
    assert gate.remaining(pending, now=pending.created_at) == 10.0
    assert gate.remaining(pending, now=pending.created_at + 4.0) == 6.0
    assert gate.remaining(pending, now=pending.created_at + 99.0) == 0.0


def test_a_stale_pending_is_not_reused(tmp_path: pathlib.Path) -> None:
    """**过期的待审批不再被复用**——这是手工验收时真踩到的坑，值得单独钉一条测试。

    症状：上一次运行留下的过期待审批被新调用复用，而等待预算从它的 `created_at` 算起，
    于是新调用**立刻**报 `approval_timeout`——人还没切到 CLI 窗口，窗口二就已经拒绝返回了。
    """
    path = tmp_path / "approvals.jsonl"
    gate = DurableApprovalGate(path, timeout_seconds=30.0)
    decision = Decision(kind="require_approval", reason_code="refund_requires_approval")
    stale = gate.register(_request({"order_id": "ord_1"}), decision, _basis())

    # 把这条记录的登记时刻挪到 10 分钟前（等价于「上一次运行留下的」）
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    rows[0]["created_at"] = rows[0]["created_at"] - 600.0
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8"
    )

    assert gate.pending() == []  # 过期的不是「待办」
    fresh = gate.register(_request({"order_id": "ord_1"}), decision, _basis())
    assert fresh.approval_id != stale.approval_id
    assert gate.remaining(fresh) > 0


# ---------------------------------------------------------------------- 端到端：两条通道


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _config(tmp_path: pathlib.Path, port: int, *, approval_timeout: float) -> GatewayConfig:
    return GatewayConfig(
        upstreams=[
            UpstreamSpec(
                alias="merchant",
                command=sys.executable,
                args=[str(MOCKS / "merchant.py")],
                env={"MOCK_MODE": "none", "MOCK_DELAY_S": "0.1"},
            ),
            UpstreamSpec(
                alias="psp",
                command=sys.executable,
                args=[str(MOCKS / "psp.py")],
                env={"PSP_MODE": "none", "MOCK_DELAY_S": "0.1"},
            ),
        ],
        port=port,
        evidence_dir=tmp_path / "evidence",
        budget_path=tmp_path / "budget.sqlite",
        approvals_path=tmp_path / "approvals.jsonl",
        policy_path=POLICY,
        mandate_path=MANDATE,
        approval_timeout_seconds=approval_timeout,
    )


@contextlib.asynccontextmanager
async def running_gateway(
    tmp_path: pathlib.Path, *, approval_timeout: float = 3.0
) -> AsyncIterator[tuple[Gateway, str]]:
    config = _config(tmp_path, _free_port(), approval_timeout=approval_timeout)
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
            yield gateway, gateway.url
        finally:
            server.should_exit = True
            await serving


async def _decide_like_a_human(gate: DurableApprovalGate, *, approved: bool, timeout: float = 3.0) -> str:
    """模拟「人在另一个窗口敲 `python -m gateway approve <id>`」：等出待审批，然后投一条决定。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pending = gate.pending()
        if pending:
            gate.decide(pending[0].approval_id, approved=approved)
            return pending[0].approval_id
        await asyncio.sleep(0.05)
    raise AssertionError("没有等到待审批记录")


def _payload(result) -> tuple[bool, str]:
    dump = result.model_dump(by_alias=True)
    texts = [block.get("text", "") for block in dump.get("content", []) if isinstance(block, dict)]
    return bool(dump.get("isError")), " | ".join(texts)


def _chain(gateway: Gateway) -> list[dict]:
    text = (gateway.config.evidence_dir / "chain.jsonl").read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


async def test_cli_channel_approves_then_continues(tmp_path: pathlib.Path) -> None:
    """兜底通道：客户端**没有** elicitation 回调 → 只带 `request_state` 的挑战；CLI 批准后继续走链。"""
    async with running_gateway(tmp_path) as (gateway, url):
        async with Client(server=url, mode="auto") as client:
            approver = asyncio.create_task(_decide_like_a_human(gateway.approval, approved=True))
            result = await client.call_tool(REFUND, {"order_id": "ord_missing", "reason": "test"})
            await approver

    is_error, text = _payload(result)
    assert is_error
    # 批准之后继续走链：上游说没有这个订单 → 工具失败，而不是策略拒绝
    assert "unknown_order" in text or "Error executing" in text
    assert "approval" not in text.lower()
    assert "require_approval" in {row["decision"] for row in _chain(gateway)}


async def test_cli_channel_decline_is_a_deny(tmp_path: pathlib.Path) -> None:
    """CLI 拒绝 = 拒绝（fail-closed），不是「等不到就当没问」。"""
    async with running_gateway(tmp_path) as (gateway, url):
        async with Client(server=url, mode="auto") as client:
            approver = asyncio.create_task(_decide_like_a_human(gateway.approval, approved=False))
            result = await client.call_tool(REFUND, {"order_id": "ord_missing", "reason": "test"})
            await approver

    is_error, text = _payload(result)
    assert is_error
    assert "approval_denied" in text


async def test_cli_channel_times_out_and_denies(tmp_path: pathlib.Path) -> None:
    """没人批准 → 超时即拒绝（ADR-0006：等待期间动作绝不执行）。"""
    async with running_gateway(tmp_path, approval_timeout=0.3) as (_gateway, url):
        async with Client(server=url, mode="auto") as client:
            result = await client.call_tool(REFUND, {"order_id": "ord_missing", "reason": "test"})

    is_error, text = _payload(result)
    assert is_error
    assert "approval_timeout" in text


async def test_legacy_client_cannot_be_asked(tmp_path: pathlib.Path) -> None:
    """老客户端连 `input_required` 都读不懂 → 没法向它提问，只能 fail-closed 拒掉。

    这条用例顺带守住**握手**：老客户端（2025-11-25）要先发 `initialize`，而拦截点必须放行它，
    否则连都连不上（实测过：`MCPError: Not implemented: initialize`）。
    """
    async with running_gateway(tmp_path) as (_gateway, url):
        async with Client(server=url, mode="legacy") as client:
            result = await client.call_tool(REFUND, {"order_id": "ord_missing", "reason": "test"})

    is_error, text = _payload(result)
    assert is_error
    assert "approval_unavailable" in text


async def test_unknown_method_is_a_structural_error(tmp_path: pathlib.Path) -> None:
    """没实现的方法一律结构化错误（fail-closed）——包括 ADR-0013 留作 future work 的 `resources/list`。

    宁可报「不认识」也不静默放行：静默放行会让 I1（不存在未经 Decision 就放行的调用）随 SDK
    版本悄悄失效。
    """
    async with running_gateway(tmp_path) as (_gateway, url):
        async with Client(server=url, mode="auto") as client:
            with pytest.raises(MCPError) as denied:
                await client.session.list_resources()

    assert denied.value.code == -32602
    assert "resources/list" in denied.value.message


async def test_form_channel_still_works(tmp_path: pathlib.Path) -> None:
    """协议通道（主）没被兜底通道挤掉：广告了 elicitation 的客户端还是走 in-band 的 form。"""
    async def approve(*args: object, **kwargs: object) -> types.ElicitResult:
        return types.ElicitResult(action="accept", content={"approve": True})

    async with running_gateway(tmp_path) as (_gateway, url):
        async with Client(server=url, mode="auto", elicitation_callback=approve) as client:
            result = await client.call_tool(REFUND, {"order_id": "ord_missing", "reason": "test"})

    is_error, text = _payload(result)
    assert is_error
    assert "unknown_order" in text or "Error executing" in text

