"""预算原子扣减（ADR-0002）——**本模块由你手写**（`AGENTS.md` 分工表）。

Step 0 只冻结接口与语义；下面的 `SqliteBudgetLedger` 是 stub，等你填实现。

语义约定（写实现时必须满足）：

1. `reserve` 是**条件更新**：`sum(committed) + sum(active holds) + amount <= limit` 才成功，
   否则抛 `BudgetExceeded`（且不留下半条 hold）。
2. 幂等键是 `(session_id, tool_call_id)`：同一键重复 `reserve` 必须返回**同一个 hold**，
   不能产生第二个——这是 T5 重复花费的正面防线。
3. `commit` / `release` 幂等；对已 commit 的 hold 再 release 必须拒绝。
4. 上游失败或超时要 `release`；`complete_checkout` 成功才 `commit`。
5. `reclaim_expired(now)` 回收超过 TTL 的 hold；**启动时要跑一次对账**（ADR-0002）。
6. 时间由调用方传入（`now`），别在实现里直接读 `time.time()`——否则重放不可复算。
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass
from typing import Literal, Protocol

__all__ = ["Balance", "BudgetExceeded", "BudgetLedger", "Hold", "HoldNotFound", "SqliteBudgetLedger"]

_TODO = "W3 Step 2：预算扣减由你实现（AGENTS.md 分工表）"


@dataclass(frozen=True)
class Hold:
    """一次 reserve 的占用（对应支付里的「授权」，不是扣款）。"""

    hold_id: str
    session_id: str
    tool_call_id: str
    amount: float
    currency: str
    created_at: float
    expires_at: float
    state: Literal["active", "committed", "released"] = "active"


@dataclass(frozen=True)
class Balance:
    committed: float
    active_holds: float
    remaining: float


class BudgetExceeded(Exception):
    """超预算：按 ADR-0007 的**可自适应层**拒绝（带稳定 `reason_code`，不回显阈值）。"""


class HoldNotFound(LookupError):
    """hold 不存在或已终结。"""


class BudgetLedger(Protocol):
    """执行点只依赖这几个方法。"""

    def reserve(
        self,
        *,
        session_id: str,
        tool_call_id: str,
        amount: float,
        currency: str,
        limit: float,
        now: float,
        ttl_seconds: float,
    ) -> Hold: ...

    def commit(self, hold_id: str, *, now: float) -> Hold: ...

    def release(self, hold_id: str, *, reason: str, now: float) -> Hold: ...

    def balance(self, session_id: str) -> Balance: ...

    def reclaim_expired(self, *, now: float) -> list[Hold]: ...


class SqliteBudgetLedger:
    """stub：W3 Step 2 由你实现（SQLite WAL + 进程内单写者 + IMMEDIATE 事务）。"""

    def __init__(self, path: pathlib.Path) -> None:
        self.path = path

    def reserve(self, **kwargs: object) -> Hold:
        raise NotImplementedError(_TODO)

    def commit(self, hold_id: str, *, now: float) -> Hold:
        raise NotImplementedError(_TODO)

    def release(self, hold_id: str, *, reason: str, now: float) -> Hold:
        raise NotImplementedError(_TODO)

    def balance(self, session_id: str) -> Balance:
        raise NotImplementedError(_TODO)

    def reclaim_expired(self, *, now: float) -> list[Hold]:
        raise NotImplementedError(_TODO)

