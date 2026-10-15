"""证据层（ADR-0003）——**本模块由你手写**（`AGENTS.md` 分工表）。

W2 的 `EvidenceLog`（JSONL 一行一条）留在这里当**临时实现**，保证网关在 W3 期间仍能跑；
W3 你要写的是 `ChainLedger`：全局哈希链 + 运行证据包（JSONL + manifest + Merkle 根）
+ `verify` + `replay`。

语义约定（写实现时必须满足）：

1. `record_pending` 必须在**转发之前**落盘（ADR-0022 的「证据先于副作用」）。
2. `record_done` 记上游摘要、脱敏标记与最终预算状态。
3. 每条记录含 `prev_hash` 与自己的 `entry_hash`；规范化用键排序（ADR-0014）。
4. **重放前置条件的最小字段集**（`docs/evaluation.md`）：规范化输入、`policy_digest`、
   预算前后、上游摘要——缺一个，I4 的「可重构」就只是口号。
5. `verify` 要能检出篡改**与截断**（截断靠运行证据包的 Merkle 根 / 锚点）。
6. deny 也要留痕（威胁模型 T9）。
"""

from __future__ import annotations

import json
import pathlib
import time
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from ..hashing import digest

__all__ = [
    "ChainLedger",
    "EvidenceEntry",
    "EvidenceLedger",
    "EvidenceLog",
    "ReplayReport",
    "VerifyReport",
    "digest",
]

_TODO = "W3 Step 3：证据链与证据包由你实现（AGENTS.md 分工表）"


@dataclass(frozen=True)
class EvidenceEntry:
    """一次被拦截调用的记录（ADR-0003 的链上单元）。"""

    phase: Literal["pending", "done"]
    seq: int
    run_id: str
    ts: float
    session_id: str
    method: str
    decision: str
    tool_call_id: str | None = None
    request_id: str | None = None
    tool: str | None = None
    upstream: str | None = None
    reason_code: str | None = None
    outcome: str | None = None
    args_digest: str | None = None
    result_digest: str | None = None
    policy_digest: str | None = None
    budget_before: float | None = None
    budget_after: float | None = None
    prev_hash: str | None = None
    entry_hash: str | None = None


@dataclass(frozen=True)
class VerifyReport:
    ok: bool
    entries: int
    broken_at: int | None = None
    detail: str = ""


@dataclass(frozen=True)
class ReplayReport:
    ok: bool
    decisions: tuple[str, ...] = ()
    mismatches: tuple[int, ...] = ()
    preconditions_met: bool = False


class EvidenceLedger(Protocol):
    """执行点只依赖这几个方法。"""

    def record_pending(self, entry: EvidenceEntry) -> EvidenceEntry: ...

    def record_done(self, entry: EvidenceEntry) -> EvidenceEntry: ...

    def verify(self, run_id: str | None = None) -> VerifyReport: ...

    def replay(self, run_id: str | None = None) -> ReplayReport: ...

    def package(self, run_id: str, target: pathlib.Path) -> pathlib.Path: ...


class ChainLedger:
    """stub：W3 Step 3 由你实现（哈希链 + 运行证据包 + verify / replay）。"""

    def __init__(self, path: pathlib.Path) -> None:
        self.path = path

    def record_pending(self, entry: EvidenceEntry) -> EvidenceEntry:
        raise NotImplementedError(_TODO)

    def record_done(self, entry: EvidenceEntry) -> EvidenceEntry:
        raise NotImplementedError(_TODO)

    def verify(self, run_id: str | None = None) -> VerifyReport:
        raise NotImplementedError(_TODO)

    def replay(self, run_id: str | None = None) -> ReplayReport:
        raise NotImplementedError(_TODO)

    def package(self, run_id: str, target: pathlib.Path) -> pathlib.Path:
        raise NotImplementedError(_TODO)


class EvidenceLog:
    """W2 的临时实现：一条记录一次被拦截的调用（没有哈希链）。

    W3 期间保留它，网关才不会因为证据层正在重写而停摆；`ChainLedger` 到位后替换。
    """

    def __init__(self, path: pathlib.Path, run_id: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.run_id = run_id
        self._fh = path.open("w", encoding="utf-8")
        self._seq = 0

    def record(self, **fields: Any) -> dict[str, Any]:
        self._seq += 1
        entry = {"seq": self._seq, "ts": round(time.time(), 3), "run_id": self.run_id, **fields}
        self._fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        self._fh.flush()
        return entry

    def close(self) -> None:
        self._fh.close()

    @staticmethod
    def load(path: pathlib.Path) -> list[dict[str, Any]]:
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

