"""审批闸门（ADR-0006 / 0021）——AI 起草，见 ADR-0026。

两条来自 ADR 的硬要求：

1. **durable wait**（ADR-0006）：待审批落盘，网关重启后仍有效；
2. **绑定身份与动作**（ADR-0021）：每条待审批都带 `subject` 与 `action_digest`，
   批准只对**那一个动作**有效——不做全局「批准」开关。

W3 先做 **CLI 兜底**通道：待审批写进 `runs/approvals.jsonl`，闸门轮询同一个文件等决定；
超时即拒绝（`approval_timeout`）。MCP 原生的 `input_required` 通道要等 MRTR 验证过了再接
（见 `docs/w3-plan.md` 的风险条）——两条通道共用同一个 `ApprovalGate` 接口。
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import time
import uuid
from dataclasses import asdict, dataclass
from typing import Protocol

from .decision import AuthorizationBasis, Decision, InterceptedRequest
from .hashing import digest

__all__ = ["ApprovalGate", "DenyAllGate", "FileApprovalGate", "PendingApproval"]


@dataclass(frozen=True)
class PendingApproval:
    approval_id: str
    subject: str
    action: str
    action_digest: str
    reason_code: str | None
    created_at: float


class ApprovalGate(Protocol):
    async def request(
        self, request: InterceptedRequest, decision: Decision, basis: AuthorizationBasis
    ) -> Decision: ...


class DenyAllGate:
    """没有人批准就拒绝。测试与「无审批通道」时的默认值。"""

    async def request(
        self, request: InterceptedRequest, decision: Decision, basis: AuthorizationBasis
    ) -> Decision:
        return Decision(kind="deny", reason_code="approval_unavailable")


class FileApprovalGate:
    """文件通道的审批闸门：`pending` 与 `decision` 两类记录写同一个 JSONL。

    `scripts/approve.py` 负责把决定追加进去——这就是 ADR-0006 说的 CLI 投递通道。
    """

    def __init__(self, path: pathlib.Path, *, timeout_seconds: float = 60.0, poll_seconds: float = 0.2) -> None:
        self.path = path
        self.timeout_seconds = timeout_seconds
        self.poll_seconds = poll_seconds
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()

    def pending(self) -> list[PendingApproval]:
        """还没被决定的待审批（`scripts/approve.py list` 用它）。"""
        rows = self._rows()
        decided = {row["approval_id"] for row in rows if row.get("kind") == "decision"}
        return [
            PendingApproval(**row)
            for row in rows
            if row.get("kind") == "pending" and row["approval_id"] not in decided
        ]

    async def request(
        self, request: InterceptedRequest, decision: Decision, basis: AuthorizationBasis
    ) -> Decision:
        pending = PendingApproval(
            approval_id=f"apr_{uuid.uuid4().hex[:10]}",
            subject=basis.subject,
            action=f"{request.method} {request.name or ''}",
            action_digest=digest({"method": request.method, "name": request.name, "arguments": request.arguments}),
            reason_code=decision.reason_code,
            created_at=round(time.time(), 3),
        )
        self._append({"kind": "pending", **asdict(pending)})
        print(f"[approval] 待审批 {pending.approval_id}：{pending.action}（{pending.reason_code}）——"
              f"`uv run python scripts/approve.py list` 查看，`approve {pending.approval_id}` 批准")

        waited = 0.0
        while waited < self.timeout_seconds:
            await asyncio.sleep(self.poll_seconds)
            waited += self.poll_seconds
            for row in self._rows():
                if row.get("kind") == "decision" and row.get("approval_id") == pending.approval_id:
                    if row.get("approved"):
                        return Decision(kind="allow")
                    return Decision(kind="deny", reason_code="approval_denied")
        return Decision(kind="deny", reason_code="approval_timeout")

    def _rows(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def _append(self, row: dict) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

