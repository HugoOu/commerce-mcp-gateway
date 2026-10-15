"""四条一致性测试（测试当 oracle）：先写、先红；W3 里实现到通过。

每条都带 `strict=True` 的 xfail——三处核心还没实现，失败是**预期状态**。等你把它们写完，
**把 xfail 标记删掉**让测试真绿，这就是「先写测试再实现」的验收方式（AGENTS.md 的 AI Native 实践 4）。
"""

from __future__ import annotations

import pathlib

import pytest

from gateway.budget import SqliteBudgetLedger
from gateway.decision import AuthorizationBasis, InterceptedRequest, Mandate
from gateway.evidence import ChainLedger, EvidenceEntry
from gateway.policy import load_policy

W3_CORE = "W3 三处核心尚未实现（AGENTS.md 分工表：policy / budget / evidence 由你手写）"


def _request(**over: object) -> InterceptedRequest:
    base: dict[str, object] = {
        "method": "tools/call",
        "session_id": "s1",
        "tool_call_id": "t1",
        "name": "merchant__create_checkout",
        "arguments": {"sku": "taco", "quantity": 1},
    }
    base.update(over)
    return InterceptedRequest(**base)  # type: ignore[arg-type]


def _basis() -> AuthorizationBasis:
    return AuthorizationBasis(
        subject="agent-1",
        scopes=frozenset({"tools:call"}),
        audience="commerce-mcp-gateway",
        mandate=Mandate(mandate_id="m1", per_txn_max=50.0, allowed_merchants=("m1",)),
    )


def _entry(**over: object) -> EvidenceEntry:
    base: dict[str, object] = {
        "phase": "pending",
        "seq": 1,
        "run_id": "r1",
        "ts": 0.0,
        "session_id": "s1",
        "method": "tools/call",
        "decision": "allow",
        "tool_call_id": "t1",
        "tool": "merchant__list_catalog",
        "args_digest": "d0",
    }
    base.update(over)
    return EvidenceEntry(**base)  # type: ignore[arg-type]


@pytest.mark.xfail(reason=W3_CORE, strict=True)
def test_deny_by_default(tmp_path: pathlib.Path) -> None:
    """没有任何 allow 规则命中 → 必须 deny，并给出稳定 reason_code（I1 / ADR-0007）。"""
    policy = load_policy(tmp_path / "policy.yaml")
    decision = policy.evaluate(_request(), _basis())
    assert decision.kind == "deny"
    assert decision.reason_code == "policy_denied"


@pytest.mark.xfail(reason=W3_CORE, strict=True)
def test_budget_charges_exactly_once(tmp_path: pathlib.Path) -> None:
    """同一 (session_id, tool_call_id) 重试多次，只产生一次 commit（I2 / ADR-0002）。"""
    ledger = SqliteBudgetLedger(tmp_path / "budget.sqlite")
    call = {
        "session_id": "s1",
        "tool_call_id": "t1",
        "amount": 30.0,
        "currency": "CNY",
        "limit": 100.0,
        "now": 0.0,
        "ttl_seconds": 60.0,
    }
    first = ledger.reserve(**call)
    again = ledger.reserve(**call)  # 同一幂等键：必须返回同一个 hold
    assert again.hold_id == first.hold_id
    ledger.commit(first.hold_id, now=1.0)
    ledger.commit(again.hold_id, now=2.0)  # 重复 commit 必须幂等
    assert ledger.balance("s1").committed == 30.0


@pytest.mark.xfail(reason=W3_CORE, strict=True)
def test_audit_covers_every_intercepted_call(tmp_path: pathlib.Path) -> None:
    """每次调用恰好 pending + done 两条；deny 也留痕；verify 必须通过（I3 / 威胁 T9）。"""
    ledger = ChainLedger(tmp_path / "evidence")
    ledger.record_pending(_entry(seq=1, phase="pending"))
    ledger.record_done(_entry(seq=1, phase="done", outcome="ok", result_digest="d1"))
    ledger.record_pending(_entry(seq=2, phase="pending", decision="deny", reason_code="policy_denied"))
    report = ledger.verify("r1")
    assert report.ok
    assert report.entries == 3


@pytest.mark.xfail(reason=W3_CORE, strict=True)
def test_replay_is_deterministic(tmp_path: pathlib.Path) -> None:
    """同一证据包重放两次，决策序列逐条一致，且重放前置条件满足（I4）。"""
    ledger = ChainLedger(tmp_path / "evidence")
    ledger.record_pending(_entry(seq=1, phase="pending", policy_digest="p0", budget_before=0.0))
    ledger.record_done(_entry(seq=1, phase="done", outcome="ok", policy_digest="p0", budget_after=9.71))
    first = ledger.replay("r1")
    second = ledger.replay("r1")
    assert first.decisions == second.decisions
    assert first.preconditions_met

