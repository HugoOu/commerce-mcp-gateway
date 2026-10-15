"""预算账本的测试（ADR-0002）——I2 的来源。

并发那条（`test_concurrent_reserves_never_exceed_limit`）是核心：50 个线程抢 100 元上限、
每笔 30 元，必须**恰好** 3 笔成功，且任何时刻都不超支。
"""

from __future__ import annotations

import concurrent.futures
import pathlib

import pytest

from gateway.budget import BudgetExceeded, HoldNotFound, SqliteBudgetLedger


def _ledger(tmp_path: pathlib.Path) -> SqliteBudgetLedger:
    return SqliteBudgetLedger(tmp_path / "budget.sqlite")


def _reserve(
    ledger: SqliteBudgetLedger,
    *,
    tool_call_id: str = "t1",
    amount: float = 30.0,
    limit: float = 100.0,
    now: float = 0.0,
    ttl_seconds: float = 60.0,
    session_id: str = "s1",
    currency: str = "CNY",
):
    return ledger.reserve(
        session_id=session_id,
        tool_call_id=tool_call_id,
        amount=amount,
        currency=currency,
        limit=limit,
        now=now,
        ttl_seconds=ttl_seconds,
    )


def test_reserve_then_commit_moves_money_from_hold_to_committed(tmp_path: pathlib.Path) -> None:
    ledger = _ledger(tmp_path)
    hold = _reserve(ledger)
    before = ledger.balance("s1")
    assert (before.committed, before.active_holds, before.remaining) == (0.0, 30.0, 70.0)

    ledger.commit(hold.hold_id, now=1.0)
    after = ledger.balance("s1")
    assert (after.committed, after.active_holds, after.remaining) == (30.0, 0.0, 70.0)


def test_same_key_returns_the_same_hold(tmp_path: pathlib.Path) -> None:
    """幂等键：同一 `(session, tool_call_id)` 不会产生第二个 hold。"""
    ledger = _ledger(tmp_path)
    first = _reserve(ledger)
    again = _reserve(ledger)
    assert again.hold_id == first.hold_id
    assert ledger.balance("s1").active_holds == 30.0


def test_same_key_with_different_amount_conflicts(tmp_path: pathlib.Path) -> None:
    """同键不同参数要冲突，不能静默按旧参数执行（照 Stripe 的做法）。"""
    ledger = _ledger(tmp_path)
    _reserve(ledger, amount=30.0)
    with pytest.raises(ValueError, match="幂等键"):
        _reserve(ledger, amount=40.0)


def test_over_limit_is_rejected_without_leaving_a_hold(tmp_path: pathlib.Path) -> None:
    ledger = _ledger(tmp_path)
    _reserve(ledger, tool_call_id="t1", amount=80.0)
    with pytest.raises(BudgetExceeded):
        _reserve(ledger, tool_call_id="t2", amount=30.0)
    balance = ledger.balance("s1")
    assert balance.active_holds == 80.0  # 失败那次没留下任何东西
    assert balance.remaining == 20.0


def test_limit_can_only_tighten(tmp_path: pathlib.Path) -> None:
    """上限取 min：成功的那次可以把上限收紧，**被拒的那次不会**，而且永远不能放宽。

    「被拒不会收紧」是刻意的：收紧发生在同一次事务里，被拒时整个事务回滚。否则任何调用方
    传一个极小的 limit 就能把会话额度锁死——那是一次廉价的 DoS。
    """
    ledger = _ledger(tmp_path)
    _reserve(ledger, tool_call_id="t1", amount=30.0, limit=100.0)
    with pytest.raises(BudgetExceeded):
        _reserve(ledger, tool_call_id="t2", amount=40.0, limit=50.0)
    assert ledger.balance("s1").remaining == 70.0  # 上限仍是 100：那次收紧被回滚了

    _reserve(ledger, tool_call_id="t3", amount=10.0, limit=50.0)  # 装得下 → 成功并收紧
    assert ledger.balance("s1").remaining == 10.0  # 50 - 30 - 10

    _reserve(ledger, tool_call_id="t4", amount=5.0, limit=200.0)  # 更大的上限不能放宽
    assert ledger.balance("s1").remaining == 5.0  # 仍是 50 的上限：50 - 30 - 10 - 5


def test_release_frees_budget(tmp_path: pathlib.Path) -> None:
    ledger = _ledger(tmp_path)
    hold = _reserve(ledger, amount=80.0)
    ledger.release(hold.hold_id, reason="upstream_failed", now=1.0)
    assert ledger.balance("s1").active_holds == 0.0
    _reserve(ledger, tool_call_id="t2", amount=80.0)  # 释放后额度回来了


def test_release_after_commit_is_rejected(tmp_path: pathlib.Path) -> None:
    ledger = _ledger(tmp_path)
    hold = _reserve(ledger)
    ledger.commit(hold.hold_id, now=1.0)
    with pytest.raises(HoldNotFound, match="commit"):
        ledger.release(hold.hold_id, reason="oops", now=2.0)


def test_commit_and_release_are_idempotent(tmp_path: pathlib.Path) -> None:
    ledger = _ledger(tmp_path)
    hold = _reserve(ledger)
    assert ledger.commit(hold.hold_id, now=1.0).state == "committed"
    assert ledger.commit(hold.hold_id, now=2.0).state == "committed"
    assert ledger.balance("s1").committed == 30.0

    other = _reserve(ledger, tool_call_id="t2", amount=10.0)
    assert ledger.release(other.hold_id, reason="a", now=1.0).state == "released"
    assert ledger.release(other.hold_id, reason="b", now=2.0).state == "released"


def test_unknown_hold_is_not_found(tmp_path: pathlib.Path) -> None:
    with pytest.raises(HoldNotFound):
        _ledger(tmp_path).commit("hold_missing", now=0.0)


def test_reclaim_expired_frees_budget(tmp_path: pathlib.Path) -> None:
    ledger = _ledger(tmp_path)
    hold = _reserve(ledger, amount=80.0, now=0.0, ttl_seconds=10.0)
    assert ledger.reclaim_expired(now=5.0) == []          # 还没过期
    reclaimed = ledger.reclaim_expired(now=11.0)
    assert [item.hold_id for item in reclaimed] == [hold.hold_id]
    assert ledger.balance("s1").active_holds == 0.0
    _reserve(ledger, tool_call_id="t2", amount=80.0)


def test_released_hold_does_not_count_toward_usage(tmp_path: pathlib.Path) -> None:
    """释放过的 hold 不再占用额度；同一幂等键仍然返回**原来那条**（重放语义）。"""
    ledger = _ledger(tmp_path)
    hold = _reserve(ledger, amount=80.0)
    ledger.release(hold.hold_id, reason="declined", now=1.0)
    replayed = _reserve(ledger, amount=80.0)
    assert replayed.hold_id == hold.hold_id and replayed.state == "released"
    assert ledger.balance("s1").active_holds == 0.0


def test_currency_mismatch_is_rejected(tmp_path: pathlib.Path) -> None:
    ledger = _ledger(tmp_path)
    _reserve(ledger, tool_call_id="t1", currency="CNY")
    with pytest.raises(ValueError, match="币种"):
        _reserve(ledger, tool_call_id="t2", currency="USD")


def test_invalid_amounts_are_rejected(tmp_path: pathlib.Path) -> None:
    ledger = _ledger(tmp_path)
    with pytest.raises(ValueError, match="amount"):
        _reserve(ledger, amount=0.0)
    with pytest.raises(ValueError, match="limit"):
        _reserve(ledger, amount=10.0, limit=0.0)


def test_state_survives_reopen(tmp_path: pathlib.Path) -> None:
    """状态留在文件里：关掉再打开，账还在（真实运行时要靠这个做重启对账）。"""
    ledger = _ledger(tmp_path)
    hold = _reserve(ledger, amount=30.0)
    ledger.commit(hold.hold_id, now=1.0)
    ledger.close()

    reopened = _ledger(tmp_path)
    assert reopened.balance("s1").committed == 30.0
    assert reopened.reclaim_expired(now=999.0) == []  # committed 不会被 TTL 回收


def test_concurrent_reserves_never_exceed_limit(tmp_path: pathlib.Path) -> None:
    """I2：50 个线程抢 100 元上限、每笔 30 元 → 恰好 3 笔成功，其余被拒。"""
    ledger = _ledger(tmp_path)
    attempts = 50

    def attempt(index: int) -> bool:
        try:
            _reserve(ledger, tool_call_id=f"t{index}", amount=30.0, limit=100.0)
        except BudgetExceeded:
            return False
        return True

    with concurrent.futures.ThreadPoolExecutor(max_workers=attempts) as pool:
        granted = sum(pool.map(attempt, range(attempts)))

    balance = ledger.balance("s1")
    assert granted == 3
    assert balance.active_holds == 90.0
    assert balance.committed + balance.active_holds <= 100.0

