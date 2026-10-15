"""预算原子扣减（ADR-0002）——实现由 AI 起草（ADR-0026），你负责检查与确认。

接口在 Step 0 冻结。语义约定（实现里逐条落地）：

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
import sqlite3
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Literal, Protocol

__all__ = ["Balance", "BudgetExceeded", "BudgetLedger", "Hold", "HoldNotFound", "SqliteBudgetLedger"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id  TEXT PRIMARY KEY,
    limit_cents INTEGER NOT NULL,
    currency    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS holds (
    hold_id        TEXT PRIMARY KEY,
    session_id     TEXT NOT NULL REFERENCES sessions(session_id),
    tool_call_id   TEXT NOT NULL,
    amount_cents   INTEGER NOT NULL,
    currency       TEXT NOT NULL,
    state          TEXT NOT NULL,              -- active | committed | released
    created_at     REAL NOT NULL,
    expires_at     REAL NOT NULL,
    settled_at     REAL,
    settled_reason TEXT,
    UNIQUE (session_id, tool_call_id)          -- 幂等键：并发重复也只会有一条
);
"""


def _to_cents(amount: float) -> int:
    """边界转换：接口用浮点（MCP 世界就是浮点），账本内部一律整数分。"""
    return int(round(amount * 100))


def _to_amount(cents: int) -> float:
    return cents / 100


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
    """SQLite（WAL）记账的预算账本。

    三条不变量（I2 的来源）：

    1. `sum(committed) + sum(active holds) ≤ limit`，**任何时刻**都成立；
    2. 同一 `(session_id, tool_call_id)` 只会产生**一个** hold（幂等键 + DB 唯一约束）；
    3. 被释放 / 回收的 hold **不再计入占用**。

    怎么做到的（每条都在挡一个具体的坑）：

    - **金额以「分」入库**（整数）：浮点求和的漂移会让 `≤ limit` 在边界上假成立；
    - **写操作一律 `BEGIN IMMEDIATE`**：先拿写锁再读，把「读余额 → 判断 → 写 hold」放进同一个事务——
      这就是 ADR-0002 点名的在途窗口（两笔 30 元并发对 100 元上限会都看到 100 可用）；
    - **进程内一把 `threading.Lock`**：SQLite 的写锁是数据库级的，进程内串行化让行为可预测；
    - **幂等靠 `UNIQUE(session_id, tool_call_id)` 兜底**：即使调用方并发重复提交，也只会插入一条。

    时间**由调用方传入**（`now`），实现里不读时钟——这样重放才可复算（architecture 第 4 条）。
    """

    def __init__(self, path: pathlib.Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        # `check_same_thread=False` 是**故意**的：账本会被并发调用（N=50 的压测就是这样），
        # 而所有访问都经过下面那把 `self._lock`——线程安全由锁保证，不靠 sqlite 的连接归属。
        self._conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        with self._lock, self._transaction():
            self._conn.executescript(SCHEMA)

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
    ) -> Hold:
        """条件更新：额度够才落 hold；同一幂等键直接返回**原来那条**。"""
        amount_cents = _to_cents(amount)
        limit_cents = _to_cents(limit)
        if amount_cents <= 0:
            raise ValueError(f"amount 必须为正：{amount!r}")
        if limit_cents <= 0:
            raise ValueError(f"limit 必须为正：{limit!r}")

        with self._lock, self._transaction():
            self._upsert_session(session_id, limit_cents, currency)
            existing = self._find_by_key(session_id, tool_call_id)
            if existing is not None:
                if existing["amount_cents"] != amount_cents or existing["currency"] != currency:
                    # 同一幂等键配不同参数：照 Stripe 的做法直接冲突，别静默按旧参数执行
                    raise ValueError(
                        f"幂等键 {tool_call_id!r} 已用于 {_to_amount(existing['amount_cents'])}"
                        f" {existing['currency']}，不能再用于 {amount} {currency}"
                    )
                return _hold_from_row(existing)

            used = self._used_cents(session_id)
            effective_limit = self._limit_cents(session_id)
            if used + amount_cents > effective_limit:
                raise BudgetExceeded(
                    f"预算不足：已占用 {_to_amount(used)} + 本次 {amount} > 上限 {_to_amount(effective_limit)}"
                )

            hold_id = f"hold_{uuid.uuid4().hex[:12]}"
            self._conn.execute(
                "INSERT INTO holds (hold_id, session_id, tool_call_id, amount_cents, currency, state,"
                "                   created_at, expires_at) VALUES (?, ?, ?, ?, ?, 'active', ?, ?)",
                (hold_id, session_id, tool_call_id, amount_cents, currency, now, now + ttl_seconds),
            )
            return _hold_from_row(self._find_by_key(session_id, tool_call_id))

    def commit(self, hold_id: str, *, now: float) -> Hold:
        """把占用转成实扣。重复 commit 幂等（返回同一条）。"""
        with self._lock, self._transaction():
            row = self._get(hold_id)
            if row["state"] == "committed":
                return _hold_from_row(row)
            if row["state"] != "active":
                raise HoldNotFound(f"{hold_id} 已经是 {row['state']}，不能再 commit")
            self._conn.execute(
                "UPDATE holds SET state='committed', settled_at=?, settled_reason=NULL WHERE hold_id=?",
                (now, hold_id),
            )
            return _hold_from_row(self._get(hold_id))

    def release(self, hold_id: str, *, reason: str, now: float) -> Hold:
        """释放占用（上游失败 / 超时 / 审批被拒）。重复 release 幂等；已 commit 的必须拒绝。"""
        with self._lock, self._transaction():
            row = self._get(hold_id)
            if row["state"] == "released":
                return _hold_from_row(row)
            if row["state"] == "committed":
                raise HoldNotFound(f"{hold_id} 已经 commit（钱已经扣了），不能再 release")
            self._conn.execute(
                "UPDATE holds SET state='released', settled_at=?, settled_reason=? WHERE hold_id=?",
                (now, reason, hold_id),
            )
            return _hold_from_row(self._get(hold_id))

    def balance(self, session_id: str) -> Balance:
        """已扣 / 在途 / 剩余。读也走同一把锁：账本只有一处真相，读一致性优先于读吞吐。"""
        with self._lock, self._transaction():
            committed = self._sum_state(session_id, "committed")
            active = self._sum_state(session_id, "active")
            limit_cents = self._limit_cents(session_id)
        return Balance(
            committed=_to_amount(committed),
            active_holds=_to_amount(active),
            remaining=_to_amount(limit_cents - committed - active),
        )

    def reclaim_expired(self, *, now: float) -> list[Hold]:
        """回收超过 TTL 的在途 hold（**启动时也要跑一次**，ADR-0002 的对账）。"""
        with self._lock, self._transaction():
            rows = self._conn.execute(
                "SELECT * FROM holds WHERE state='active' AND expires_at <= ? ORDER BY created_at",
                (now,),
            ).fetchall()
            for row in rows:
                self._conn.execute(
                    "UPDATE holds SET state='released', settled_at=?, settled_reason='ttl_expired'"
                    " WHERE hold_id=?",
                    (now, row["hold_id"]),
                )
        return [
            _hold_from_row({**dict(row), "state": "released", "settled_reason": "ttl_expired"})
            for row in rows
]

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------ 内部工具

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        """`BEGIN IMMEDIATE`：先拿写锁再读，避免「读了再写」的竞态。"""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")

    def _upsert_session(self, session_id: str, limit_cents: int, currency: str) -> None:
        """记录会话的上限与币种。上限**只能收紧**（取 min），与 ADR-0020 同一条原则。"""
        row = self._conn.execute(
            "SELECT limit_cents, currency FROM sessions WHERE session_id=?", (session_id,)
        ).fetchone()
        if row is None:
            self._conn.execute(
                "INSERT INTO sessions (session_id, limit_cents, currency) VALUES (?, ?, ?)",
                (session_id, limit_cents, currency),
            )
            return
        if row["currency"] != currency:
            raise ValueError(f"会话 {session_id} 的币种是 {row['currency']}，不能混用 {currency}")
        if limit_cents < row["limit_cents"]:
            self._conn.execute(
                "UPDATE sessions SET limit_cents=? WHERE session_id=?", (limit_cents, session_id)
            )

    def _limit_cents(self, session_id: str) -> int:
        row = self._conn.execute(
            "SELECT limit_cents FROM sessions WHERE session_id=?", (session_id,)
        ).fetchone()
        return int(row["limit_cents"]) if row is not None else 0

    def _find_by_key(self, session_id: str, tool_call_id: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM holds WHERE session_id=? AND tool_call_id=?", (session_id, tool_call_id)
        ).fetchone()

    def _get(self, hold_id: str) -> sqlite3.Row:
        row = self._conn.execute("SELECT * FROM holds WHERE hold_id=?", (hold_id,)).fetchone()
        if row is None:
            raise HoldNotFound(hold_id)
        return row

    def _used_cents(self, session_id: str) -> int:
        """占用 = 已扣 + 在途；released 不算（不变量 3）。"""
        return self._sum_state(session_id, "committed") + self._sum_state(session_id, "active")

    def _sum_state(self, session_id: str, state: str) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) AS total FROM holds WHERE session_id=? AND state=?",
            (session_id, state),
        ).fetchone()
        return int(row["total"])


def _hold_from_row(row: Any) -> Hold:
    return Hold(
        hold_id=row["hold_id"],
        session_id=row["session_id"],
        tool_call_id=row["tool_call_id"],
        amount=_to_amount(row["amount_cents"]),
        currency=row["currency"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        state=row["state"],
    )

