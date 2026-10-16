"""审批闸门（ADR-0006 / 0021）——AI 起草，见 ADR-0026。

两条来自 ADR 的硬要求：

1. **durable wait**（ADR-0006）：待审批落盘，网关重启后仍有效；
2. **绑定身份与动作**（ADR-0021）：每条待审批都带 `subject` 与 `action_digest`，
   批准只对**那一个动作**有效——不做全局「批准」开关。

两条通道，同一个闸门：

- **协议通道（主）**：网关返回 `input_required`，客户端把回答带回 `params["inputResponses"]`；
  形状是实测出来的，见 `docs/w3-plan.md` 的「Step 4 的 MRTR 验证」小节。
- **文件 / CLI 通道（兜底）**：`scripts/approve.py` 往同一个 JSONL 追加决定，闸门轮询它；
  客户端不支持 elicitation 时用这条，超时即拒绝（`approval_timeout`）。
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import time
import uuid
from dataclasses import asdict, dataclass, fields
from typing import Protocol

import mcp_types as types

from .decision import AuthorizationBasis, Decision, InterceptedRequest
from .hashing import digest

__all__ = ["APPROVAL_KEY", "ApprovalGate", "DenyAllGate", "DurableApprovalGate", "PendingApproval"]

APPROVAL_KEY = "approve"
"""挑战与回答共用的键：网关用它在 `inputRequests` 里提问，也从 `inputResponses` 里取回答。"""


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


class DurableApprovalGate:
    """durable 审批闸门：`pending` 与 `decision` 两类记录写同一个 JSONL（ADR-0006 的 durable wait）。

    `scripts/approve.py` 负责把决定追加进去——这就是 ADR-0006 说的 CLI 投递通道。
    """

    def __init__(self, path: pathlib.Path, *, timeout_seconds: float = 60.0, poll_seconds: float = 0.2) -> None:
        self.path = path
        self.timeout_seconds = timeout_seconds
        self.poll_seconds = poll_seconds
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()

    # ------------------------------------------------------------------ 协议通道（主）

    def register(
        self, request: InterceptedRequest, decision: Decision, basis: AuthorizationBasis
    ) -> PendingApproval:
        """落一条 durable 待审批记录。绑定身份与动作（ADR-0021）：批准只对**那一个动作**有效。"""
        pending = PendingApproval(
            approval_id=f"apr_{uuid.uuid4().hex[:10]}",
            subject=basis.subject,
            action=f"{request.method} {request.name or ''}".strip(),
            action_digest=digest({"method": request.method, "name": request.name, "arguments": request.arguments}),
            reason_code=decision.reason_code,
            created_at=round(time.time(), 3),
        )
        self._append({"kind": "pending", **asdict(pending)})
        print(
            f"[approval] 待审批 {pending.approval_id}：{pending.action}（{pending.reason_code}）——"
            f"`uv run python scripts/approve.py list` 查看，`approve {pending.approval_id}` 批准"
        )
        return pending

    def challenge(self, pending: PendingApproval, request: InterceptedRequest) -> types.InputRequiredResult:
        """把问题交给客户端。`request_state` 承载 `approval_id`——它是客户端回传的不透明句柄。"""
        return types.InputRequiredResult(
            input_requests={
                APPROVAL_KEY: types.ElicitRequest(
                    params=types.ElicitRequestFormParams(
                        message=f"批准这次调用吗？{pending.action}（{pending.reason_code}）",
                        requested_schema={
                            "type": "object",
                            "properties": {"approve": {"type": "boolean"}},
                            "required": ["approve"],
                        },
                    )
                )
            },
            request_state=pending.approval_id,
        )

    def interpret(self, answer: dict) -> Decision:
        """读客户端的回答：`accept` 且 `approve=True` 才放行，其余一律拒（fail-closed）。"""
        if answer.get("action") == "accept" and (answer.get("content") or {}).get("approve") is True:
            return Decision(kind="allow")
        return Decision(kind="deny", reason_code="approval_denied")

    # ------------------------------------------------------------------ 文件 / CLI 通道（兜底）

    def pending(self) -> list[PendingApproval]:
        """还没被决定的待审批（`scripts/approve.py list` 用它）。"""
        rows = self._rows()
        decided = {row["approval_id"] for row in rows if row.get("kind") == "decision"}
        known = {field.name for field in fields(PendingApproval)}
        return [
            PendingApproval(**{key: value for key, value in row.items() if key in known})
            for row in rows
            if row.get("kind") == "pending" and row["approval_id"] not in decided
        ]

    async def request(
        self, request: InterceptedRequest, decision: Decision, basis: AuthorizationBasis
    ) -> Decision:
        # 落盘与协议通道共用 `register()`：两条通道必须记录同一个形状，
        # 否则 CLI 看到的和客户端批准的可能对不上。
        pending = self.register(request, decision, basis)

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

