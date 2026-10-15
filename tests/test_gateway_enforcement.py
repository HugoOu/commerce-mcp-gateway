"""Step 4 验收：三条越权通道全部被拦，且都能在证据里查到（ADR-0007 / 0011 / 0023）。

这是**确定性**验收——不经过 LLM，直接构造请求。比让模型去撞墙可靠得多，也更容易回归。
三条通道：① 白名单外的工具；② 超上限下单；③ 越界 URI 读取。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import pathlib
import socket
import sys
from collections.abc import AsyncIterator

import pytest
import uvicorn
from mcp.client.client import Client
from mcp.shared.exceptions import MCPError

from gateway.app import Gateway
from gateway.config import GatewayConfig, UpstreamSpec

ROOT = pathlib.Path(__file__).resolve().parents[1]
MOCKS = ROOT / "src" / "mcp_servers"
POLICY = ROOT / "src" / "gateway" / "policy" / "demo-policy.yaml"
MANDATE = ROOT / "src" / "gateway" / "demo-mandate.yaml"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _config(tmp_path: pathlib.Path, port: int, psp_mode: str = "none") -> GatewayConfig:
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
                env={"PSP_MODE": psp_mode, "MOCK_DELAY_S": "0.1"},
            ),
        ],
        port=port,
        evidence_dir=tmp_path / "evidence",
        budget_path=tmp_path / "budget.sqlite",
        approvals_path=tmp_path / "approvals.jsonl",
        policy_path=POLICY,
        mandate_path=MANDATE,
        approval_timeout_seconds=0.5,
    )


@contextlib.asynccontextmanager
async def running_gateway(tmp_path: pathlib.Path, psp_mode: str = "none") -> AsyncIterator[tuple[Gateway, str]]:
    config = _config(tmp_path, _free_port(), psp_mode)
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


def _chain(gateway: Gateway) -> list[dict]:
    text = (gateway.config.evidence_dir / "chain.jsonl").read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _payload(result) -> tuple[bool, str]:
    dump = result.model_dump(by_alias=True)
    texts = [block.get("text", "") for block in dump.get("content", []) if isinstance(block, dict)]
    return bool(dump.get("isError")), " | ".join(texts)


async def test_tools_list_is_filtered(tmp_path: pathlib.Path) -> None:
    """ADR-0010 / 0011：被规则拒绝的工具不出现在清单里。"""
    async with running_gateway(tmp_path) as (_gateway, url):
        async with Client(server=url, mode="auto") as client:
            tools = [tool.name for tool in (await client.list_tools()).tools]
    assert "merchant__list_catalog" in tools
    assert "psp__refund_payment" not in tools


async def test_channel_one_tool_outside_the_policy(tmp_path: pathlib.Path) -> None:
    """通道①：白名单外的工具 → `isError` + 稳定 reason_code（**不是**协议错误，ADR-0007 第 2 层）。"""
    async with running_gateway(tmp_path) as (gateway, url):
        async with Client(server=url, mode="auto") as client:
            result = await client.call_tool("psp__refund_payment", {"payment_intent_id": "pi_x"})

    is_error, text = _payload(result)
    assert is_error
    assert "policy_denied" in text
    rows = _chain(gateway)
    assert any(row["decision"] == "deny" for row in rows)
    assert gateway.evidence.verify(gateway.config.run_id).ok


async def test_channel_two_over_limit_payment(tmp_path: pathlib.Path) -> None:
    """通道②：超上限下单 → 拒绝，且**不回显阈值**（ADR-0007 的「不成为攻击预言机」）。"""
    async with running_gateway(tmp_path) as (gateway, url):
        async with Client(server=url, mode="auto") as client:
            result = await client.call_tool(
                "psp__create_payment_intent",
                {"amount": 999.0, "merchant_id": "m1", "checkout_ref": "ck_x", "idempotency_key": "k1"},
            )
        # 账本在 `async with` 退出时就关了，所以要在里面读（这是第一次踩的坑）
        balance = gateway.budget.balance("agent-dev")

    is_error, text = _payload(result)
    assert is_error
    assert "amount_over_limit" in text
    assert "999" not in text
    assert "50" not in text  # mandate 的单笔上限是 50，绝不能出现在线上理由里
    assert balance.active_holds == 0.0  # 被拒的调用不留下 hold
    rows = _chain(gateway)
    assert any(row["reason_code"] == "amount_over_limit" for row in rows)


async def test_channel_three_out_of_mandate_resource_read(tmp_path: pathlib.Path) -> None:
    """通道③：越界 URI 与「不存在」**同形**——`-32602` + 同样的消息（ADR-0023）。"""
    async with running_gateway(tmp_path) as (gateway, url):
        async with Client(server=url, mode="auto") as client:
            with pytest.raises(MCPError) as denied:
                await client.session.read_resource("mcp://m1/orders/secret")
            with pytest.raises(MCPError) as missing:
                await client.session.read_resource("mcp://m1/orders/does-not-exist")

    assert denied.value.code == missing.value.code == -32602
    assert denied.value.message == missing.value.message == "Resource not found"
    rows = _chain(gateway)
    assert any(row["method"] == "resources/read" for row in rows)


async def test_allowed_payment_goes_through_and_settles(tmp_path: pathlib.Path) -> None:
    """正路也要通：允许的支付走完整链，成功即 commit（ADR-0002）。"""
    async with running_gateway(tmp_path) as (gateway, url):
        async with Client(server=url, mode="auto") as client:
            result = await client.call_tool(
                "psp__create_payment_intent",
                {"amount": 9.71, "merchant_id": "m1", "checkout_ref": "ck_ok", "idempotency_key": "k-ok"},
            )
        balance = gateway.budget.balance("agent-dev")

    is_error, text = _payload(result)
    assert not is_error, text
    assert balance.committed == 9.71
    assert balance.active_holds == 0.0


async def test_declined_payment_releases_the_hold(tmp_path: pathlib.Path) -> None:
    """业务失败不是工具失败：调用成功、但 hold 必须 release（`--psp-mode decline` 的那条路径）。"""
    async with running_gateway(tmp_path, psp_mode="decline") as (gateway, url):
        async with Client(server=url, mode="auto") as client:
            result = await client.call_tool(
                "psp__create_payment_intent",
                {"amount": 9.71, "merchant_id": "m1", "checkout_ref": "ck_d", "idempotency_key": "k-d"},
            )
        balance = gateway.budget.balance("agent-dev")

    is_error, text = _payload(result)
    assert not is_error, text  # PSP 是「调用成功 + 状态为失败」
    assert balance.committed == 0.0
    assert balance.active_holds == 0.0  # 被拒 → release，不占额度
    assert any(row["outcome"] == "declined" for row in _chain(gateway))


async def test_every_intercepted_call_lands_in_the_chain(tmp_path: pathlib.Path) -> None:
    """审计完整 + 重放前置条件：链自洽，且每条记录都够重放用。"""
    async with running_gateway(tmp_path) as (gateway, url):
        async with Client(server=url, mode="auto") as client:
            await client.list_tools()
            await client.call_tool("psp__refund_payment", {"payment_intent_id": "pi_x"})
            await client.call_tool(
                "psp__create_payment_intent",
                {"amount": 9.71, "merchant_id": "m1", "checkout_ref": "ck_ok", "idempotency_key": "k-ok"},
            )

    run_id = gateway.config.run_id
    report = gateway.evidence.verify(run_id)
    assert report.ok, report.detail
    # 一次 tools/list（done）+ 一次被拒（pending）+ 一次允许（pending + done）= 4
    assert report.entries >= 4
    decisions = {row["decision"] for row in _chain(gateway)}
    assert {"allow", "deny"} <= decisions
    assert gateway.evidence.replay(run_id).preconditions_met

