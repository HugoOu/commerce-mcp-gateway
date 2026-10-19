"""执行点（I1）：所有被拦截的方法调用都从这里过一遍——AI 起草，分工见 ADR-0026。

完整链路（architecture §5）：

    authorize → decide → enforce → record_pending → forward → sanitize → settle → record_done

分工（ADR-0005）：决策逻辑只在这里，前门与中间件一行都不碰；上游客户端只在 `upstream.py` 构造。

三个容易被追问的点：

1. **拒绝形态**（ADR-0007 三层）：可自适应 → 返回 `CallToolResult(is_error=True)` + 稳定
   `reason_code` + 一句「怎么改」的提示；硬安全 → 只给通用码；结构性 → 抛 `MCPError`；
2. **证据先于副作用**（ADR-0022）：`record_pending` 一定在转发之前；
3. **净化不改决策**（I1）：净化在转发之后跑，命中注入只标记，不改变这次调用的判定。
"""

from __future__ import annotations

import itertools
import json
import os
import time
from typing import Any, NoReturn

import mcp_types as types
from mcp.shared.exceptions import MCPError
from mcp_types import INVALID_PARAMS, INVALID_REQUEST

from . import telemetry
from .approval import APPROVAL_KEY, ApprovalChannel, DurableApprovalGate
from .budget import BudgetExceeded, BudgetLedger, Hold
from .decision import AuthorizationBasis, Decision, InterceptedRequest
from .evidence import ChainLedger, EvidenceEntry, ReconstructReport
from .hashing import digest
from .identity import IdentityResolver
from .policy import PolicyEngine
from .sanitize import Sanitizer
from .transaction import TransactionGuard
from .upstream import UnknownTool, UpstreamRegistry

GENERIC_DENIAL = "policy_denied"
"""硬安全层的线上理由：只给通用码，不给阈值、白名单与规则名（ADR-0007）。"""

ADAPTIVE_HINTS: dict[str, str] = {
    "amount_over_limit": "金额超过授权上限，换一个更小的金额再试",
    "amount_unreadable": "amount 必须是数字",
    "merchant_not_allowed": "这个商户不在授权范围内",
    "merchant_missing": "缺少 merchant_id",
    "category_not_allowed": "这个品类不在授权范围内",
    "budget_exceeded": "本次运行的预算已经用尽",
    "no_mandate": "没有可用的 mandate，无法授权任何花费",
    "mandate_expired": "mandate 已过期",
    "approval_denied": "人工审批拒绝了这次调用",
    "approval_timeout": "等待人工审批超时",
    "approval_unavailable": "这个客户端没法接受人工审批请求（需要 elicitation 能力或现代版协议）",
}
"""可自适应层的提示：只说**怎么改**，不回显阈值与规则名（ADR-0007 第 2 层）。"""


class _Denied(Exception):
    """内部信号：把「占额度时被拒」的判定带回调用点，转成线上形态。"""

    def __init__(self, decision: Decision) -> None:
        super().__init__(decision.reason_code)
        self.decision = decision


class Enforcer:
    """唯一执行点。"""

    def __init__(
        self,
        *,
        registry: UpstreamRegistry,
        evidence: ChainLedger,
        policy: PolicyEngine,
        budget: BudgetLedger,
        identity: IdentityResolver,
        sanitizer: Sanitizer,
        transactions: TransactionGuard,
        approval: DurableApprovalGate,
        run_id: str,
        list_ttl_ms: int = 30_000,
        hold_ttl_seconds: float = 120.0,
    ) -> None:
        self._registry = registry
        self._evidence = evidence
        self._policy = policy
        self._budget = budget
        self._identity = identity
        self._sanitizer = sanitizer
        self._transactions = transactions
        self._approval = approval
        self._run_id = run_id
        self._list_ttl_ms = list_ttl_ms
        self._hold_ttl_seconds = hold_ttl_seconds
        self._seq = itertools.count(1)

    # ------------------------------------------------------------------ 身份

    def basis(self, token: str | None) -> AuthorizationBasis | None:
        return self._identity.basis(token)

    def unauthenticated(self) -> NoReturn:
        """身份缺失或认不出来：**结构性错误**——客户端该修的是凭证，不是模型该修的东西。"""
        raise MCPError(code=INVALID_REQUEST, message="unauthenticated", data=None)

    # ------------------------------------------------------------------ 重放重构

    def reconstruct(self, run_id: str) -> ReconstructReport:
        """用**真策略**把这次运行的决策逐条重算（ADR-0028 的可重构性）。

        评估器闭包住策略与身份解析——证据层不 import 策略（ADR-0003 的分层），所以
        「怎么重算」由执行点决定，「能不能重算」由证据层如实报告（两个数分开报）。

        三个输入都从证据里还原：`arguments` 来自可复算输入件、`now` 就是条目的 `ts`
        （`_record` 用的就是求值时刻）、`basis` 按主体反查身份配置（token 本身不进证据）。
        """

        def evaluator(entry: EvidenceEntry, arguments: dict[str, Any]) -> str:
            basis = self._identity.basis_for_subject(entry.session_id)
            if basis is None:
                return "unknown_subject"
            if entry.method == "tools/call":
                try:
                    self._registry.resolve(entry.tool or "")
                except UnknownTool:
                    return "error"  # 未知工具那条记录的 decision 就是 error（结构性错误）
            request = InterceptedRequest(
                method=entry.method,
                session_id=entry.session_id,
                tool_call_id=entry.tool_call_id or "",
                request_id=entry.request_id,
                name=entry.tool,
                arguments=arguments,
                now=entry.ts,
            )
            return self._policy.evaluate(request, basis).kind

        return self._evidence.reconstruct(run_id, evaluator=evaluator)

    # ------------------------------------------------------------------ 工具清单

    async def list_tools(self, basis: AuthorizationBasis) -> types.ListToolsResult:
        """过滤后的清单（ADR-0010 / 0011）+ 缓存提示（ADR-0010：一律收紧为 private）。"""
        tools = await self._registry.list_tools()
        visible = [strip_upstream_hints(tool) for tool in tools if self._visible(tool.name, basis)]
        self._record(
            self._request("tools/list", None, {}, basis, None, time.time()),
            "done",
            seq=next(self._seq),
            decision="allow",
            outcome="ok",
            policy_digest=self._policy.digest,
            result_digest=digest([tool.name for tool in visible]),
        )
        telemetry.annotate("tools/list", decision="allow", outcome="ok")
        return types.ListToolsResult(tools=visible, ttl_ms=self._list_ttl_ms, cache_scope="private")

    def _visible(self, tool_name: str, basis: AuthorizationBasis) -> bool:
        """只摘掉被规则**直接拒绝**的工具。

        用「探针参数」（金额 0、商户取 mandate 里第一个）让参数类约束不误伤——列表阶段还不知道
        真实参数，所以这里只问「这条路径是不是被策略封了」。更细的按参数过滤留 future work。
        """
        probe = self._probe_arguments(tool_name, basis)
        request = self._request("tools/call", tool_name, probe, basis, None, time.time())
        return self._policy.evaluate(request, basis).kind != "deny"

    def _probe_arguments(self, tool_name: str, basis: AuthorizationBasis) -> dict[str, Any]:
        binding = self._policy.binding_for(tool_name)
        mandate = basis.mandate
        probe: dict[str, Any] = {}
        if binding.amount_field:
            probe[binding.amount_field] = 0.0
        if binding.merchant_field and mandate and mandate.allowed_merchants:
            probe[binding.merchant_field] = mandate.allowed_merchants[0]
        if binding.category_field and mandate and mandate.allowed_categories:
            probe[binding.category_field] = mandate.allowed_categories[0]
        return probe

    # ------------------------------------------------------------------ 工具调用

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        basis: AuthorizationBasis,
        request_id: str | int | None = None,
        input_responses: dict[str, Any] | None = None,
        request_state: str | None = None,
        approval_channel: ApprovalChannel = "none",
        now: float | None = None,
    ) -> types.CallToolResult | types.InputRequiredResult:
        """返回 `CallToolResult`；需要人工审批时返回 `InputRequiredResult`（MRTR，ADR-0006）。

        `approval_channel` 由前门按入站 `_meta` 判（`approval.channel_for`）——执行点不解析
        `_meta`，它只按「这个客户端能接受哪种通道」办事。默认 `none` 是 **fail-closed**：
        没人告诉执行点客户端能不能回答，就当不能。
        """
        moment = time.time() if now is None else now
        seq = next(self._seq)
        request = self._request("tools/call", name, arguments, basis, request_id, moment)

        # ① 判定（含审批闸门）——拒绝在这里就返回，不碰上游、不碰预算
        decision = self._policy.evaluate(request, basis)
        if decision.kind == "require_approval":
            outcome = await self._resolve_approval(
                request,
                decision,
                basis,
                seq=seq,
                input_responses=input_responses,
                request_state=request_state,
                channel=approval_channel,
            )
            if isinstance(outcome, types.InputRequiredResult):
                return outcome
            decision = outcome
        if decision.kind != "allow":
            return self._deny(request, decision, seq=seq)

        # ①.5 交易上下文（ADR-0029）：per-call 都合法，不代表整条交易合法。
        blocked = self._transactions.check(name, arguments, basis)
        if blocked is not None:
            return self._deny(
                request,
                Decision(kind="deny", reason_code=blocked, hard=False),
                seq=seq,
            )

        # ② 预算：有金额的调用先占额度（ADR-0002 的 reserve）
        try:
            hold = self._reserve(request, basis)
        except _Denied as denied:
            return self._deny(request, denied.decision, seq=seq)
        before = self._used(request.session_id)

        # ③ 找上游（找不到是结构性错误：`CallToolResult` 文档说 finding the tool 要报 MCP error）
        try:
            upstream, real_name = self._registry.resolve(name)
        except UnknownTool:
            self._record(request, "pending", seq=seq, decision="error", outcome="unknown_tool",
                         policy_digest=self._policy.digest)
            raise MCPError(code=INVALID_PARAMS, message=f"Unknown tool: {name}", data=name) from None

        # ④ 证据先于副作用（ADR-0022）
        self._record(request, "pending", seq=seq, decision="allow", policy_digest=self._policy.digest,
                     budget_before=before)

        # ⑤ 转发
        try:
            raw = await upstream.call_tool(real_name, arguments)
        except Exception:
            if hold is not None:
                self._budget.release(hold.hold_id, reason="upstream_error", now=moment)
            self._record(request, "done", seq=seq, decision="allow", outcome="upstream_error",
                         policy_digest=self._policy.digest, budget_before=before,
                         budget_after=self._used(request.session_id))
            raise

        # ⑥ 净化（不改决策，只标记）
        sanitized, report = self._sanitizer.apply(raw)
        declined = _is_business_failure(sanitized)

        # ⑦ 结算：成功 commit，失败 / 被拒 release（ADR-0002）
        if hold is not None:
            if sanitized.is_error or declined:
                self._budget.release(hold.hold_id, reason="declined" if declined else "tool_error", now=moment)
            else:
                self._budget.commit(hold.hold_id, now=moment)

        # ⑦.5 交易状态只在成功结果上推进（ADR-0029）。
        if not sanitized.is_error and not declined:
            payload = _json_payload(sanitized)
            if payload is not None:
                self._transactions.observe(name, payload, basis)

        # ⑧ 收尾记账
        outcome = "declined" if declined else ("tool_error" if sanitized.is_error else "ok")
        self._record(request, "done", seq=seq, decision="allow", outcome=outcome,
                     policy_digest=self._policy.digest, budget_before=before,
                     budget_after=self._used(request.session_id),
                     result_digest=digest(sanitized.model_dump(by_alias=True).get("content")),
                     sanitized=report.changed,
                     suspected_adversarial=report.flagged_injection)
        telemetry.annotate("tools/call", tool=name, upstream=upstream.alias, decision="allow",
                           outcome=outcome, duration_ms=0.0)
        return sanitized

    async def _resolve_approval(
        self,
        request: InterceptedRequest,
        decision: Decision,
        basis: AuthorizationBasis,
        *,
        seq: int,
        input_responses: dict[str, Any] | None,
        request_state: str | None,
        channel: ApprovalChannel,
    ) -> Decision | types.InputRequiredResult:
        """把 `require_approval` 落成一个 `allow` / `deny`，或返回要发回客户端的挑战（ADR-0006）。

        四步，按通道分叉：

        1. 这一轮带了客户端的回答 → 直接判（协议通道的第二轮）；
        2. 客户端连现代版都不是 → 它读不懂 `input_required`，我们没法向它提问，fail-closed 拒掉；
        3. 第一轮 → 落 durable 待审批记录：能 elicitation 就给 form 挑战，否则给只带
           `request_state` 的挑战（兜底通道）；
        4. 兜底通道的第二轮 → 在**这一次请求里**等文件里的决定（durable wait）。

        为什么第 4 步的等待要放在网关侧？因为 SDK 的 state-only 重试上限是 10 轮、退避
        50ms→250ms（≈1.3s）——人来不及在 CLI 上敲一条命令。等待预算从**登记时刻**算起
        （`DurableApprovalGate.remaining`），所以客户端多试几轮也续不了命。
        """
        answer = (input_responses or {}).get(APPROVAL_KEY)
        if answer is not None:
            verdict = self._approval.interpret(answer)
            answered = self._approval.resume(request_state, request, basis)
            if answered is not None:
                # 协议通道也要落一条决定：不落的话这条待审批会永远留在「待办」里（实测踩到过）
                self._approval.decide(
                    answered.approval_id, approved=verdict.kind == "allow", by="client"
                )
            return verdict

        if channel == "none":
            return Decision(kind="deny", reason_code="approval_unavailable")

        pending = self._approval.resume(request_state, request, basis)
        if pending is None:
            pending = self._approval.register(request, decision, basis)
            self._record(request, "pending", seq=seq, decision="require_approval",
                         reason_code=decision.reason_code, policy_digest=self._policy.digest)
            telemetry.annotate(request.method, tool=request.name, decision="require_approval",
                               outcome="challenged")
            if channel == "form":
                return self._approval.challenge(pending, request)
            return self._approval.state_only(pending)

        if channel == "form":
            # 问过一轮却没带回回答——再问一次（客户端自己的重试上限会兜住死循环）
            return self._approval.challenge(pending, request)

        verdict = await self._approval.wait(
            pending.approval_id, timeout=self._approval.remaining(pending)
        )
        if verdict is None:
            # 超时也是一次「解决」：不落这条，它会一直挂在待办列表里，而且会被下一次调用复用
            self._approval.decide(pending.approval_id, approved=False, by="timeout")
            return Decision(kind="deny", reason_code="approval_timeout")
        return verdict

    # ------------------------------------------------------------------ 资源

    async def read_resource(
        self,
        uri: str,
        *,
        basis: AuthorizationBasis,
        request_id: str | int | None = None,
        now: float | None = None,
    ) -> types.ReadResourceResult:
        """W3 **不代理 resources**（ADR-0012 不声明该能力）：一律按 ADR-0023 的形状回 `-32602`。

        「越界读取」与「不存在」**同形**——这是刻意的：不给存在性预言机。判定照样留痕。
        """
        moment = time.time() if now is None else now
        seq = next(self._seq)
        request = self._request("resources/read", uri, {}, basis, request_id, moment)
        decision = self._policy.evaluate(request, basis)
        self._record(
            request,
            "done" if decision.kind == "allow" else "pending",
            seq=seq,
            decision=decision.kind,
            outcome="not_found",
            reason_code=decision.reason_code,
            policy_digest=self._policy.digest,
            result_digest=digest({"uri": uri, "found": False}),
        )
        raise MCPError(code=INVALID_PARAMS, message="Resource not found", data={"uri": uri})

    # ------------------------------------------------------------------ 内部工具

    def _request(
        self,
        method: str,
        name: str | None,
        arguments: dict[str, Any],
        basis: AuthorizationBasis,
        request_id: str | int | None,
        now: float,
    ) -> InterceptedRequest:
        """把一次入站调用变成执行点认识的东西。

        - `session_id` 绑到**调用方身份**（ADR-0017：协议层没有会话概念，这是我们自己定的口径）；
        - `tool_call_id` 由 (会话, 方法, 名字, 参数) 的摘要派生：**同样的调用重试 = 同一个 hold**，
          参数变了就是新的一次调用。代价是「同一会话里两次参数完全相同的合法调用」会被当成一次
          重试——需要客户端提供 attempt id 才能真正区分，留到 W4（见 w3-plan 的「本阶段不做」）。
        """
        return InterceptedRequest(
            method=method,
            session_id=basis.subject,
            tool_call_id=digest({"session": basis.subject, "method": method, "name": name, "arguments": arguments}),
            request_id=None if request_id is None else str(request_id),
            name=name,
            arguments=arguments,
            now=now,
        )

    def _reserve(self, request: InterceptedRequest, basis: AuthorizationBasis) -> Hold | None:
        """**动钱**的调用先占额度（ADR-0027：授权只是 hold，不占）。

        两个条件都要满足：绑定声明了金额字段（否则不知道占多少），且 `reserves_budget` 没被关掉。
        没有 mandate 就不许花钱（fail-closed）。
        """
        binding = self._policy.binding_for(request.name or "")
        if not binding.amount_field or not binding.reserves_budget:
            return None
        amount = request.arguments.get(binding.amount_field)
        if amount is None:
            # 走不到这里：**策略引擎已经先拒了**——`_check_constraints` 在绑定声明了金额字段而参数里
            # 没有金额时返回 `amount_unreadable`（fail-closed）。留这一行只为「没有金额就不占额度」
            # 这个局部不变量自明，不重复判定（判定只有一处，ADR-0005 的分工）。
            return None
        if isinstance(amount, bool) or not isinstance(amount, (int, float)):
            raise _Denied(Decision(kind="deny", reason_code="amount_unreadable"))
        mandate = basis.mandate
        if mandate is None or mandate.budget_total is None:
            raise _Denied(Decision(kind="deny", reason_code="no_mandate"))
        try:
            return self._budget.reserve(
                session_id=request.session_id,
                tool_call_id=request.tool_call_id,
                amount=float(amount),
                currency=mandate.currency,
                limit=mandate.budget_total,
                now=request.now or time.time(),
                ttl_seconds=self._hold_ttl_seconds,
            )
        except BudgetExceeded as exc:
            raise _Denied(Decision(kind="deny", reason_code="budget_exceeded")) from exc

    def _used(self, session_id: str) -> float:
        balance = self._budget.balance(session_id)
        return round(balance.committed + balance.active_holds, 2)

    def _deny(self, request: InterceptedRequest, decision: Decision, *, seq: int) -> types.CallToolResult:
        """ADR-0007 的第 2 层：**返回** `is_error=True` 的结果，而不是抛异常。

        硬安全层只给通用码；可自适应层给稳定 `reason_code` + 一句「怎么改」——两者都不回显阈值。
        """
        reason = GENERIC_DENIAL if decision.hard else (decision.reason_code or GENERIC_DENIAL)
        hint = "" if decision.hard else ADAPTIVE_HINTS.get(reason, "")
        # 实验用的「错误消息注入」：把修复建议挂在网关**自己的**拒绝提示后面。
        # 这是比商品文案更靠近信任边界的位置——模型对工具/网关错误的信任通常更高。
        # 默认不设，所以产品行为不变；F6 族用它。
        suffix = os.environ.get("GATEWAY_HINT_SUFFIX", "").strip()
        if hint and suffix:
            hint = f"{hint}；{suffix}"
        self._record(request, "pending", seq=seq, decision="deny", reason_code=decision.reason_code,
                     outcome="denied", policy_digest=self._policy.digest)
        telemetry.annotate(request.method, tool=request.name, decision="deny", outcome="denied")
        message = f"denied: {reason}" + (f" —— {hint}" if hint else "")
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=message)],
            is_error=True,
        )

    def _record(
        self,
        request: InterceptedRequest,
        phase: str,
        *,
        seq: int,
        decision: str,
        outcome: str | None = None,
        reason_code: str | None = None,
        policy_digest: str | None = None,
        budget_before: float | None = None,
        budget_after: float | None = None,
        result_digest: str | None = None,
        sanitized: bool = False,
        suspected_adversarial: bool = False,
    ) -> EvidenceEntry:
        # 可复算输入**单独**落一份（ADR-0028）：链上只留摘要，重放时用摘要把两件钉在一起。
        # 幂等——一次调用有 pending / done 两次记账，但输入只有一份。
        self._evidence.record_inputs(self._run_id, seq, request.arguments)
        entry = EvidenceEntry(
            phase="pending" if phase == "pending" else "done",
            seq=seq,
            run_id=self._run_id,
            ts=round(request.now or time.time(), 3),
            session_id=request.session_id,
            method=request.method,
            decision=decision,
            tool_call_id=request.tool_call_id,
            request_id=request.request_id,
            tool=request.name,
            upstream=self._upstream_of(request.name),
            reason_code=reason_code,
            outcome=outcome,
            args_digest=digest(request.arguments),
            result_digest=result_digest,
            policy_digest=policy_digest,
            budget_before=budget_before,
            budget_after=budget_after,
            sanitized=sanitized,
            suspected_adversarial=suspected_adversarial,
        )
        return self._evidence.record_pending(entry) if phase == "pending" else self._evidence.record_done(entry)

    def _upstream_of(self, name: str | None) -> str | None:
        if not name:
            return None
        try:
            return self._registry.resolve(name)[0].alias
        except UnknownTool:
            return None


def _json_payload(result: types.CallToolResult) -> dict[str, Any] | None:
    """工具结果的文本 JSON（mock 世界统一返回 JSON 文本）；解析失败返回 None。"""
    for block in result.content:
        text = getattr(block, "text", None)
        if not isinstance(text, str):
            continue
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _is_business_failure(result: types.CallToolResult) -> bool:
    """业务失败不是工具失败（`docs/mock-world.md` §6）：载荷里出现 `last_payment_error` 就是被拒。"""
    for block in result.content:
        text = getattr(block, "text", None)
        if isinstance(text, str) and "last_payment_error" in text:
            return True
    return False


def strip_upstream_hints(tool: types.Tool) -> types.Tool:
    """出口**剥离**上游的 `annotations`（ADR-0012 的子决策：不原样透传上游 hint）。

    为什么剥离而不是「标记为不可信」：客户端拿到 hint 就会照它自动放行——`readOnlyHint` 的语义
    正是「这个工具不改环境，不必问人」。少给一个字段，比多给一个必须被怀疑的字段更安全。
    （网关用自己的判定覆盖上游注解是 ADR-0012 留的后续扩展，本版不做。）
    """
    if tool.annotations is None:
        return tool
    return tool.model_copy(update={"annotations": None})

