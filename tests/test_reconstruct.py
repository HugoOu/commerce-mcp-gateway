"""重放重构（ADR-0028）：链上只留摘要、可复算输入单独落盘，**两个数分开报**。

这个文件守的是**可重构性**——不是「重放结果一致」，而是「证据够不够把决策重算出来」。
最值钱的一条是 `test_withdrawn_inputs_keep_the_chain_verifiable`：撤下输入件后链仍然可验，
而前置条件满足率归零——那个落差就是文献说的 overclaim gap，是一条**可报**的结论。
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

from harness import jsonl_rows, running_gateway
from mcp.client.client import Client

from gateway.evidence import ChainLedger, EvidenceEntry, digest

REFUND = "psp__refund_payment"
OK_PAYMENT = {"amount": 9.71, "merchant_id": "m1", "checkout_ref": "ck_ok", "idempotency_key": "k-ok"}


def _inputs_path(gateway: Any, run_id: str) -> pathlib.Path:
    return gateway.config.evidence_dir / "runs" / f"{run_id}.inputs.jsonl"


async def _flow(gateway: Any, url: str) -> None:
    """一次「清单 + 允许 + 拒绝」的混合运行：重构要同时覆盖三种路径。"""
    async with Client(server=url, mode="auto") as client:
        await client.list_tools()                                            # 决策由执行点写，跳过
        await client.call_tool("psp__create_payment_intent", OK_PAYMENT)     # allow
        await client.call_tool(REFUND, {"payment_intent_id": "pi_x"})        # deny


async def test_inputs_are_recorded_but_never_on_the_chain(tmp_path: pathlib.Path) -> None:
    """可复算输入**不进链**：链是可对外出示的防篡改件，金额与商户不该出现在那里。"""
    async with running_gateway(tmp_path) as (gateway, url):
        await _flow(gateway, url)
        run_id = gateway.config.run_id
        inputs = jsonl_rows(_inputs_path(gateway, run_id))
        chain = jsonl_rows(gateway.config.evidence_dir / "runs" / f"{run_id}.jsonl")

    assert inputs, "输入件要落下来"
    assert any(row["arguments"].get("amount") == 9.71 for row in inputs)
    assert all("arguments" not in row for row in chain), "链上只能有 args_digest，不能有原文"
    assert all(row["args_digest"] for row in chain if row["method"] == "tools/call")


async def test_reconstruction_reproduces_the_real_decisions(tmp_path: pathlib.Path) -> None:
    """用真策略重算：前置条件与决策都要对得上——两个数都是 1.0。"""
    async with running_gateway(tmp_path) as (gateway, url):
        await _flow(gateway, url)
        report = gateway.enforcer.reconstruct(gateway.config.run_id)

    assert report.eligible >= 2, "至少要有一次允许与一次拒绝"
    assert report.precondition_rate == 1.0, report.detail
    assert report.reconstruction_rate == 1.0, report.detail
    assert report.mismatches == ()
    assert any("tools/list" in reason for _seq, reason in report.skipped)


async def test_tampered_inputs_are_detected(tmp_path: pathlib.Path) -> None:
    """改了输入件 → 摘要对不上 → **不算可重构**，而不是被悄悄采信。"""
    async with running_gateway(tmp_path) as (gateway, url):
        await _flow(gateway, url)
        run_id = gateway.config.run_id
        path = _inputs_path(gateway, run_id)
        rows = jsonl_rows(path)
        # 改**支付那条**（第一条是 tools/list，它按方法就被跳过了，改了也测不到东西）
        target = next(row for row in rows if "amount" in row["arguments"])
        target["arguments"]["amount"] = 0.01
        path.write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8"
        )
        report = gateway.enforcer.reconstruct(run_id)

    assert report.precondition_rate < 1.0
    assert any("args_digest" in reason for _seq, reason in report.skipped)


async def test_withdrawn_inputs_keep_the_chain_verifiable(tmp_path: pathlib.Path) -> None:
    """**撤下输入件**：链仍然可验，但重构能力归零——落差可测，这才是分层的意义（ADR-0028）。"""
    async with running_gateway(tmp_path) as (gateway, url):
        await _flow(gateway, url)
        run_id = gateway.config.run_id
        _inputs_path(gateway, run_id).unlink()
        report = gateway.enforcer.reconstruct(run_id)
        verified = gateway.evidence.verify(run_id)

    assert verified.ok, verified.detail
    assert report.eligible >= 2
    assert report.precondition_rate == 0.0
    assert report.reconstruction_rate == 0.0


def test_a_wrong_decision_is_reported_as_a_mismatch(tmp_path: pathlib.Path) -> None:
    """重算出来与链上不一致 → 记进 mismatches（**不隐藏**），而不是当作通过。"""
    ledger = ChainLedger(tmp_path / "evidence")
    ledger.record_pending(
        EvidenceEntry(
            phase="pending",
            seq=1,
            run_id="r1",
            ts=0.0,
            session_id="agent-dev",
            method="tools/call",
            decision="allow",
            tool_call_id="t1",
            tool="psp__capture_payment",
            args_digest=digest({"amount": 9.71}),
        )
    )
    ledger.record_inputs("r1", 1, {"amount": 9.71})

    def wrong_evaluator(_entry: EvidenceEntry, _arguments: dict[str, Any]) -> str:
        return "deny"

    report = ledger.reconstruct("r1", evaluator=wrong_evaluator)
    assert report.precondition_rate == 1.0
    assert report.mismatches == (1,)
    assert report.reconstruction_rate == 0.0

