"""最小证据记录（W2 占位版）。

W2 只做「每次被拦截的调用一条 JSONL」：进出摘要 + 时间 + 结果。
哈希链与运行证据包（ADR-0003）由 W3 手写替换本模块——见 AGENTS.md 的分工表，
`evidence/` 的三处核心之一属于你。

摘要口径按 ADR-0014：键排序后的规范化 JSON 再取 sha256 前 16 位，保证同一逻辑报文
的不同序列化得到同一摘要。
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import time
from typing import Any


def digest(value: Any) -> str:
    """规范化摘要：键排序 + 紧凑分隔符（ADR-0014 的透传义务之一）。"""
    blob = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


class EvidenceLog:
    """JSONL 账本：一条记录一次被拦截的调用。"""

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

