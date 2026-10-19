"""三个测试文件共用的脚手架：真网关 + 真 mock 上游 + 真 HTTP。

抽出来的时机是「第三处出现」——`test_gateway_enforcement.py` 与 `test_approval_channels.py`
各有一份本地副本，`test_adversarial.py` 是第三处。

放在 `tests/` 而不是 `src/`：这是**测试**脚手架，不进产品包（`pyproject.toml` 的 wheel 清单里
没有它）。导入靠 pytest 的 rootdir 插入（默认 importmode=prepend），所以 `tests/` 下不要加
`__init__.py`。

用法：

    async def test_x(tmp_path):
        async with running_gateway(tmp_path, psp_mode="decline") as (gateway, url):
            ...
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import pathlib
import re
import socket
import sys
from collections.abc import AsyncIterator
from typing import Any

import uvicorn

from gateway.app import Gateway
from gateway.config import GatewayConfig, UpstreamSpec
from gateway.identity import IdentityConfig

ROOT = pathlib.Path(__file__).resolve().parents[1]
MOCKS = ROOT / "src" / "mcp_servers"
POLICY = ROOT / "src" / "gateway" / "policy" / "demo-policy.yaml"
MANDATE = ROOT / "src" / "gateway" / "demo-mandate.yaml"


def free_port() -> int:
    """让 OS 挑一个空闲端口——写死端口会撞上本机残留进程（W2 踩过）。"""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def config(
    tmp_path: pathlib.Path,
    *,
    port: int | None = None,
    psp_mode: str = "none",
    merchant_mode: str = "none",
    psp_hints: str = "",
    approval_timeout: float = 3.0,
    identity: IdentityConfig | None = None,
    merchant_env: dict[str, str] | None = None,
    transaction_guard: bool = False,
) -> GatewayConfig:
    """演示策略 + 两个 mock 上游 + 所有产物落在 tmp_path（测试之间互不干扰）。

    `psp_hints="lie"` 让 mock PSP 给扣款工具声明「只读」注解——T1 的靶子。
    """
    merchant = {"MOCK_MODE": merchant_mode, "MOCK_DELAY_S": "0.1"}
    if merchant_env:
        merchant.update(merchant_env)
    return GatewayConfig(
        upstreams=[
            UpstreamSpec(
                alias="merchant",
                command=sys.executable,
                args=[str(MOCKS / "merchant.py")],
                env=merchant,
            ),
            UpstreamSpec(
                alias="psp",
                command=sys.executable,
                args=[str(MOCKS / "psp.py")],
                env={"PSP_MODE": psp_mode, "MOCK_DELAY_S": "0.1", "MOCK_HINTS": psp_hints},
            ),
        ],
        port=port if port is not None else free_port(),
        evidence_dir=tmp_path / "evidence",
        budget_path=tmp_path / "budget.sqlite",
        approvals_path=tmp_path / "approvals.jsonl",
        policy_path=POLICY,
        mandate_path=MANDATE,
        approval_timeout_seconds=approval_timeout,
        identity=identity or IdentityConfig(),
        transaction_guard=transaction_guard,
    )


@contextlib.asynccontextmanager
async def running_gateway(
    tmp_path: pathlib.Path, **overrides: Any
) -> AsyncIterator[tuple[Gateway, str]]:
    """起一个真网关（真 stdio mock 上游 + 真 Streamable HTTP），yield `(gateway, url)`。

    注意账本在 `async with` 退出时就关了：余额要在块**里面**读（W3 踩过）。
    """
    conf = config(tmp_path, **overrides)
    async with Gateway(conf) as gateway:
        assert gateway.app is not None
        server = uvicorn.Server(
            uvicorn.Config(gateway.app, host=conf.host, port=conf.port, log_level="warning")
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


def payload(result: Any) -> tuple[bool, str]:
    """把 `CallToolResult` 摊成 `(isError, 全部文本拼起来)`——断言只看这两样。"""
    dump = result.model_dump(by_alias=True)
    texts = [block.get("text", "") for block in dump.get("content", []) if isinstance(block, dict)]
    return bool(dump.get("isError")), " | ".join(texts)


def jsonl_rows(path: pathlib.Path) -> list[dict]:
    if not path.exists():
        return []
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def chain_rows(gateway: Gateway) -> list[dict]:
    """全局证据链（含 deny 与历史运行）。"""
    return jsonl_rows(gateway.config.evidence_dir / "chain.jsonl")


def intent_id(result: Any) -> str:
    """从授权结果里取出 `payment_intent_id`——捕获要用它（mock 不认假 id）。"""
    _, text = payload(result)
    found = re.search(r'"payment_intent_id":\s*"(pi_[0-9a-f]+)"', text)
    assert found, f"授权结果里没有 payment_intent_id：{text[:200]}"
    return found.group(1)


async def pay(
    client: Any,
    *,
    amount: float = 9.71,
    checkout_ref: str = "ck_ok",
    key: str = "k-ok",
) -> tuple[Any, Any]:
    """跑一遍「授权 → 捕获」，返回两次结果。

    **为什么要两步**（ADR-0027）：只有**真正动钱**的调用占额度——授权只是 hold，捕获才进账。
    所以任何「钱被记了」的断言都必须发生在捕获之后，一笔购买也只会记一次。
    """
    authorized = await client.call_tool(
        "psp__create_payment_intent",
        {
            "amount": amount,
            "merchant_id": "m1",
            "checkout_ref": checkout_ref,
            "idempotency_key": f"{key}-auth",
        },
    )
    captured = await client.call_tool(
        "psp__capture_payment",
        {
            "payment_intent_id": intent_id(authorized),
            "amount": amount,
            "idempotency_key": f"{key}-cap",
        },
    )
    return authorized, captured

