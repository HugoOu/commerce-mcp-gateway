"""审批闸门（ADR-0006 / 0021）——AI 起草，见 ADR-0026。

两条来自 ADR 的硬要求：

1. **durable wait**（ADR-0006）：待审批落盘，网关重启后仍有效；
2. **绑定身份与动作**（ADR-0021）：每条待审批都带 `subject` 与 `action_digest`，
   批准只对**那一个动作**有效——不做全局「批准」开关。

三条通道，同一个闸门（走哪条由 `channel_for()` 从入站 `_meta` 判）：

- **协议通道（主）**：客户端注册了 elicitation 回调 → 网关返回带 `input_requests` 的
  `InputRequiredResult`（form 模式，ADR-0021），回答落在 `params["inputResponses"]`；
- **协议通道的兜底形态**：客户端**没有** elicitation 回调 → 返回**只带 `request_state`** 的
  `InputRequiredResult`，客户端退避重试，网关在**第二轮**里等文件里的决定。为什么不在每一轮
  都立刻返回？因为 SDK 的重试上限是 10 轮、退避 50ms→250ms（≈1.3s），人来不及在 CLI 上敲一条
  命令——所以等待必须发生在网关这一侧，这正是 durable wait 的含义；
- **文件 / CLI 通道**：`python -m gateway approve <id>` 往同一个 JSONL 追加决定。

三条通道写的是**同一个** JSONL、同一种形状——CLI 看到的和客户端批准的一定是同一件事。
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import time
import uuid
from dataclasses import asdict, dataclass, fields
from typing import Any, Literal

import mcp_types as types

from .decision import AuthorizationBasis, Decision, InterceptedRequest
from .hashing import digest

__all__ = [
    "APPROVAL_KEY",
    "MODERN_ERA",
    "ApprovalChannel",
    "DurableApprovalGate",
    "PendingApproval",
    "channel_for",
]

APPROVAL_KEY = "approve"
"""挑战与回答共用的键：网关用它在 `input_requests` 里提问，也从 `inputResponses` 里取回答。"""

MODERN_ERA = "2026-07-28"
"""现代版协议（ADR-0024）：`input_required` / MRTR 从这一版起才有。"""

ApprovalChannel = Literal["form", "cli", "none"]


@dataclass(frozen=True)
class PendingApproval:
    approval_id: str
    subject: str
    action: str
    action_digest: str
    reason_code: str | None
    created_at: float


def channel_for(meta: dict[str, Any] | None) -> ApprovalChannel:
    """这个客户端能接受哪种审批通道。

    形状是实测的（`docs/w3-plan.md` 的 MRTR 小节）：`_meta` 里的
    `io.modelcontextprotocol/clientCapabilities` **只在真的注册了回调时才出现**
    （`ClientSession._build_capabilities`：回调是默认值就广告 `None`），
    所以「广告了 elicitation」是可信的能力信号，不是猜测。

    顺序是刻意的，两处都是 fail-closed：

    1. **先看协议时代**——老客户端读不懂 `input_required`，问它等于发一个它解析不了的结果；
    2. 再看 elicitation——没有就退到兜底通道（`cli`），而不是放弃审批。

    版本号是 `YYYY-MM-DD`，字典序即时间序，所以直接比字符串。
    """
    if not isinstance(meta, dict):
        return "none"
    version = meta.get("io.modelcontextprotocol/protocolVersion")
    if not isinstance(version, str) or version < MODERN_ERA:
        return "none"
    capabilities = meta.get("io.modelcontextprotocol/clientCapabilities")
    if isinstance(capabilities, dict) and capabilities.get("elicitation"):
        return "form"
    return "cli"


def _action_digest(request: InterceptedRequest) -> str:
    """动作指纹：方法 + 工具名 + 参数（ADR-0021 要求批准绑定到具体动作）。"""
    return digest({"method": request.method, "name": request.name, "arguments": request.arguments})


def _pending_from_row(row: dict[str, Any]) -> PendingApproval:
    """还原一条待审批——只取数据类认识的字段（记录里还带 `kind`）。"""
    known = {field.name for field in fields(PendingApproval)}
    return PendingApproval(**{key: value for key, value in row.items() if key in known})


class DurableApprovalGate:
    """durable 审批闸门：`pending` 与 `decision` 两类记录写同一个 JSONL（ADR-0006 的 durable wait）。

    幂等：同一个（动作指纹, 主体）在**未决定**期间只有一条待审批——客户端重试与并发的第二轮
    不会在 CLI 里刷出一串重复条目。已决定的不复用（ADR-0021 的「单次使用」）。
    """

    def __init__(
        self, path: pathlib.Path, *, timeout_seconds: float = 60.0, poll_seconds: float = 0.2
    ) -> None:
        self.path = path
        self.timeout_seconds = timeout_seconds
        self.poll_seconds = poll_seconds
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()

    # ------------------------------------------------------------------ 协议通道（主）

    def register(
        self, request: InterceptedRequest, decision: Decision, basis: AuthorizationBasis
    ) -> PendingApproval:
        """落一条 durable 待审批记录（幂等）。绑定身份与动作（ADR-0021）。"""
        action_digest = _action_digest(request)
        for item in self.pending():
            if item.action_digest == action_digest and item.subject == basis.subject:
                return item
        pending = PendingApproval(
            approval_id=f"apr_{uuid.uuid4().hex[:10]}",
            subject=basis.subject,
            action=f"{request.method} {request.name or ''}".strip(),
            action_digest=action_digest,
            reason_code=decision.reason_code,
            created_at=round(time.time(), 3),
        )
        self._append({"kind": "pending", **asdict(pending)})
        print(
            f"[approval] 待审批 {pending.approval_id}：{pending.action}（{pending.reason_code}）——"
            f"`uv run python -m gateway approvals` 查看，`approve {pending.approval_id}` 批准"
        )
        return pending

    def resume(
        self,
        request_state: str | None,
        request: InterceptedRequest,
        basis: AuthorizationBasis,
    ) -> PendingApproval | None:
        """按客户端回传的 `request_state` 找回待审批。

        `request_state` 是**客户端给的**，规格只说「原样回传」——所以必须核对动作指纹与主体
        （ADR-0021 的绑定要求），对不上就当没这回事，重新登记。否则一个恶意客户端可以拿着
        别人的 `request_state` 把自己的调用接到别人批准过的动作上。
        """
        if not request_state:
            return None
        for row in self._rows():
            if row.get("kind") != "pending" or row.get("approval_id") != request_state:
                continue
            item = _pending_from_row(row)
            if item.action_digest == _action_digest(request) and item.subject == basis.subject:
                return item
            return None
        return None

    def challenge(
        self, pending: PendingApproval, request: InterceptedRequest
    ) -> types.InputRequiredResult:
        """把问题交给客户端（form 模式）。`request_state` 承载 `approval_id`——客户端回传的句柄。"""
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

    def state_only(self, pending: PendingApproval) -> types.InputRequiredResult:
        """只带 `request_state` 的挑战（兜底通道）：客户端不能 elicitation，问题只能出在带外。

        规格要求两者至少有一个，所以「只有 `request_state`」是合法形状。客户端会退避重试，
        网关在下一轮里等文件里的决定。
        """
        return types.InputRequiredResult(request_state=pending.approval_id)

    def interpret(self, answer: dict) -> Decision:
        """读客户端的回答：`accept` 且 `approve=True` 才放行，其余一律拒（fail-closed）。"""
        if answer.get("action") == "accept" and (answer.get("content") or {}).get("approve") is True:
            return Decision(kind="allow")
        return Decision(kind="deny", reason_code="approval_denied")

    # ------------------------------------------------------------------ 文件 / CLI 通道（兜底）

    def pending(self, *, now: float | None = None) -> list[PendingApproval]:
        """**还有效**的待审批：未决定，且等待预算还没过期（`python -m gateway approvals` 用它）。

        过期的那些不再列出来、也不再被复用——它们是历史记录，不是待办。
        为什么必须这样分？因为验收时真踩到过一个很隐蔽的坑：上一次运行留下的**过期待审批**被新
        调用复用，而等待预算从它的 `created_at` 算起，于是新调用**立刻**报 `approval_timeout`
        ——人还没切到 CLI 窗口，窗口二就已经拒绝返回了。
        """
        moment = time.time() if now is None else now
        rows = self._rows()
        decided = {row["approval_id"] for row in rows if row.get("kind") == "decision"}
        items = [
            _pending_from_row(row)
            for row in rows
            if row.get("kind") == "pending" and row["approval_id"] not in decided
        ]
        return [item for item in items if self.remaining(item, now=moment) > 0]

    def decision_for(self, approval_id: str) -> Decision | None:
        """文件里已经给出的决定；还没给就返回 `None`。"""
        for row in self._rows():
            if row.get("kind") != "decision" or row.get("approval_id") != approval_id:
                continue
            if row.get("approved"):
                return Decision(kind="allow")
            return Decision(kind="deny", reason_code="approval_denied")
        return None

    def decide(self, approval_id: str, *, approved: bool, by: str = "human") -> None:
        """投递一条决定——**所有**通道的收尾都走这里，形状因此只有一处定义。

        `by` 记的是「谁结的这件事」：`human`（CLI 通道）/ `client`（协议通道里客户端答的）
        / `timeout`（等超了，fail-closed 拒掉）。三者都写 `approved`，所以 `decision_for`
        不需要区分；但审计要区分——「人工拒绝」与「没人理」不是同一件事。

        为什么超时也要落一条：不落的话这条待审批会**永远**留在「待办」列表里（协议通道同样如此），
        而下一轮调用还可能复用它。
        """
        self._append(
            {
                "kind": "decision",
                "approval_id": approval_id,
                "approved": approved,
                "decided_by": by,
                "decided_at": round(time.time(), 3),
            }
        )
    async def wait(self, approval_id: str, *, timeout: float) -> Decision | None:
        """等文件里出现决定——**durable wait 就落在这一句**。超时返回 `None`，由调用方拒掉。"""
        waited = 0.0
        while True:
            verdict = self.decision_for(approval_id)
            if verdict is not None:
                return verdict
            if waited >= timeout:
                return None
            await asyncio.sleep(self.poll_seconds)
            waited += self.poll_seconds

    def remaining(self, pending: PendingApproval, *, now: float | None = None) -> float:
        """这条待审批还剩多少等待预算（`created_at + timeout`；过期即 0）。

        等待预算从**登记时刻**算起，不是从这一轮请求算起——否则客户端每重试一次就能续一次命。
        """
        moment = time.time() if now is None else now
        return max(0.0, pending.created_at + self.timeout_seconds - moment)

    # ------------------------------------------------------------------ 存储

    def _rows(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [
            json.loads(line)
            for line in self.path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def _append(self, row: dict) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

