"""最小交易上下文（ADR-0029 的 demo 版）。

只做四件事，不做通用工作流：

1. 一个 checkout 只允许一个有效 payment intent；
2. intent / capture 金额必须等于权威购物车总额（demo 版，不支持部分捕获）；
3. `complete_checkout` 前必须有成功 capture，且 token 必须精确等于 PSP 签发值；
4. 每个 mandate 默认只允许一次交易，挡住拆单/重复消费。

**明确 TODO**：SQLite 持久化、崩溃恢复、部分捕获/multicapture、退款累计的完整语义、
并发 TOCTOU、上游签名 token 验证。demo 先证明「per-call 之外还需要 transaction context」。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

from .decision import AuthorizationBasis

__all__ = ["CheckoutState", "TransactionGuard"]


def _same_money(left: float, right: float) -> bool:
    """金额比较按分（0.005 容差），避免浮点尾差。"""
    return abs(float(left) - float(right)) < 0.005


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


@dataclass
class CheckoutState:
    checkout_id: str
    total: float
    currency: str
    merchant_id: str
    sku: str
    mandate_id: str | None
    status: str = "open"  # open | captured | completed | cancelled | refunded
    intent_id: str | None = None
    payment_token: str | None = None
    pending_intent: bool = False
    pending_capture: bool = False
    authorized_amount: float = 0.0
    captured_amount: float = 0.0
    refunded_amount: float = 0.0
    order_id: str | None = None
    history: list[str] = field(default_factory=list)


class TransactionGuard:
    """进程内交易账本（demo 版）。所有方法都在同一把锁下，避免 check/observe 交错。"""

    def __init__(self, *, enabled: bool = True) -> None:
        self.enabled = enabled
        self._lock = threading.Lock()
        self._checkouts: dict[str, CheckoutState] = {}
        self._intent_to_checkout: dict[str, str] = {}
        self._started_by_mandate: dict[str, int] = {}

    # ------------------------------------------------------------------ 决策前

    def check(self, name: str, arguments: dict[str, Any], basis: AuthorizationBasis) -> str | None:
        """返回拒绝理由码；None = 放行。只服务固定的 commerce/pay 工具。"""
        if not self.enabled:
            return None
        with self._lock:
            if name == "merchant__create_payment_intent":
                return None  # 别名不存在，防御性放行；真实工具名在下面
            if name == "psp__create_payment_intent":
                return self._check_intent(arguments, basis)
            if name == "psp__capture_payment":
                return self._check_capture(arguments)
            if name == "merchant__complete_checkout":
                return self._check_complete(arguments)
            if name == "merchant__cancel_checkout":
                return self._check_cancel(arguments)
            if name == "psp__refund_payment":
                return self._check_refund(arguments)
            return None

    def _check_intent(self, arguments: dict[str, Any], basis: AuthorizationBasis) -> str | None:
        checkout_id = arguments.get("checkout_ref")
        state = self._checkouts.get(str(checkout_id)) if isinstance(checkout_id, str) else None
        if state is None:
            return "unknown_checkout_ref"
        if state.status != "open":
            return "checkout_not_open"
        if state.intent_id is not None or state.pending_intent:
            return "duplicate_payment_intent"
        amount = _number(arguments.get("amount"))
        if amount is None or not _same_money(amount, state.total):
            return "amount_mismatch"
        currency = str(arguments.get("currency") or state.currency)
        if currency != state.currency:
            return "currency_mismatch"
        merchant = arguments.get("merchant_id")
        if merchant is not None and merchant != state.merchant_id:
            return "merchant_mismatch"
        mandate = basis.mandate
        if mandate is not None and mandate.max_transactions is not None:
            started = self._started_by_mandate.get(mandate.mandate_id, 0)
            if started >= mandate.max_transactions:
                return "mandate_transaction_limit"
        # 在 check 内占位，堵住 check → 上游 → observe 之间的并发窗口。
        state.pending_intent = True
        return None

    def _check_capture(self, arguments: dict[str, Any]) -> str | None:
        intent_id = arguments.get("payment_intent_id")
        state = self._state_for_intent(str(intent_id)) if isinstance(intent_id, str) else None
        if state is None:
            return "unknown_payment_intent"
        if state.pending_capture or state.status in ("captured", "completed", "refunded"):
            return "payment_already_captured"
        if state.status != "open":
            return "checkout_not_open"
        amount = _number(arguments.get("amount"))
        if amount is None or not _same_money(amount, state.total):
            return "amount_mismatch"
        state.pending_capture = True
        return None

    def _check_complete(self, arguments: dict[str, Any]) -> str | None:
        checkout_id = arguments.get("checkout_id")
        state = self._checkouts.get(str(checkout_id)) if isinstance(checkout_id, str) else None
        if state is None:
            return "unknown_checkout_ref"
        if state.status == "completed":
            return "checkout_already_completed"
        if state.status == "cancelled":
            return "checkout_not_open"
        if state.status != "captured":
            return "payment_not_captured"
        if not _same_money(state.captured_amount, state.total):
            return "payment_not_captured"
        token = arguments.get("payment_token")
        if not isinstance(token, str) or token != state.payment_token:
            return "token_mismatch"
        return None

    def _check_cancel(self, arguments: dict[str, Any]) -> str | None:
        checkout_id = arguments.get("checkout_id")
        state = self._checkouts.get(str(checkout_id)) if isinstance(checkout_id, str) else None
        if state is None:
            return "unknown_checkout_ref"
        if state.status in ("captured", "completed", "refunded"):
            return "checkout_has_capture"
        if state.status != "open":
            return "checkout_not_open"
        return None

    def _check_refund(self, arguments: dict[str, Any]) -> str | None:
        intent_id = arguments.get("payment_intent_id")
        state = self._state_for_intent(str(intent_id)) if isinstance(intent_id, str) else None
        if state is None:
            return "unknown_payment_intent"
        if state.status not in ("captured", "completed", "refunded"):
            return "payment_not_captured"
        amount = _number(arguments.get("amount"))
        if amount is None:
            amount = state.captured_amount - state.refunded_amount
        if amount > state.captured_amount - state.refunded_amount + 0.005:
            return "refund_exceeds_captured"
        return None

    # ------------------------------------------------------------------ 结果后

    def observe(self, name: str, payload: dict[str, Any], basis: AuthorizationBasis) -> None:
        """上游成功后更新状态。解析失败就不更新——后续 check 会 fail-closed。"""
        if not self.enabled or not payload:
            return
        with self._lock:
            if name == "merchant__create_checkout":
                self._observe_checkout(payload, basis)
            elif name == "psp__create_payment_intent":
                self._observe_intent(payload)
            elif name == "psp__capture_payment":
                self._observe_capture(payload)
            elif name == "merchant__complete_checkout":
                self._observe_complete(payload)
            elif name == "merchant__cancel_checkout":
                self._observe_cancel(payload)
            elif name == "psp__refund_payment":
                self._observe_refund(payload)

    def _observe_checkout(self, payload: dict[str, Any], basis: AuthorizationBasis) -> None:
        checkout_id = payload.get("checkout_id")
        totals = payload.get("totals") or {}
        total = _number(totals.get("total")) if isinstance(totals, dict) else None
        if not isinstance(checkout_id, str) or total is None:
            return
        line_items = payload.get("line_items") or []
        sku = ""
        if isinstance(line_items, list) and line_items and isinstance(line_items[0], dict):
            sku = str(line_items[0].get("sku") or "")
        mandate = basis.mandate
        self._checkouts[checkout_id] = CheckoutState(
            checkout_id=checkout_id,
            total=total,
            currency=str(payload.get("currency") or "CNY"),
            merchant_id=str(payload.get("merchant_id") or ""),
            sku=sku,
            mandate_id=mandate.mandate_id if mandate else None,
            history=["checkout_created"],
        )

    def _observe_intent(self, payload: dict[str, Any]) -> None:
        intent_id = payload.get("payment_intent_id")
        checkout_id = payload.get("checkout_ref")
        if not isinstance(intent_id, str) or not isinstance(checkout_id, str):
            return
        state = self._checkouts.get(checkout_id)
        if state is None or state.intent_id is not None:
            return
        state.intent_id = intent_id
        state.pending_intent = False
        state.payment_token = str(payload.get("payment_token") or "")
        state.authorized_amount = _number(payload.get("amount")) or 0.0
        state.history.append("intent_created")
        self._intent_to_checkout[intent_id] = checkout_id
        if state.mandate_id is not None:
            self._started_by_mandate[state.mandate_id] = self._started_by_mandate.get(state.mandate_id, 0) + 1

    def _observe_capture(self, payload: dict[str, Any]) -> None:
        intent_id = payload.get("payment_intent_id")
        state = self._state_for_intent(str(intent_id)) if isinstance(intent_id, str) else None
        if state is None:
            return
        captured = _number(payload.get("captured_amount"))
        if captured is None:
            captured = _number(payload.get("amount")) or 0.0
        state.captured_amount = captured
        state.pending_capture = False
        state.status = "captured"
        state.history.append("captured")

    def _observe_complete(self, payload: dict[str, Any]) -> None:
        checkout_id = payload.get("checkout_id")
        state = self._checkouts.get(str(checkout_id)) if isinstance(checkout_id, str) else None
        if state is None:
            return
        state.status = "completed"
        state.order_id = str(payload.get("order_id") or "")
        state.history.append("completed")

    def _observe_cancel(self, payload: dict[str, Any]) -> None:
        checkout_id = payload.get("checkout_id")
        state = self._checkouts.get(str(checkout_id)) if isinstance(checkout_id, str) else None
        if state is None:
            return
        state.status = "cancelled"
        state.history.append("cancelled")

    def _observe_refund(self, payload: dict[str, Any]) -> None:
        intent_id = payload.get("payment_intent_id")
        state = self._state_for_intent(str(intent_id)) if isinstance(intent_id, str) else None
        if state is None:
            return
        amount = _number(payload.get("amount")) or 0.0
        state.refunded_amount = min(state.captured_amount, state.refunded_amount + amount)
        if state.refunded_amount >= state.captured_amount - 0.005:
            state.status = "refunded"
        state.history.append("refunded")

    # ------------------------------------------------------------------ 测试/观测

    def abort(self, name: str, arguments: dict[str, Any]) -> None:
        """上游失败/业务拒绝时释放 check 占位，允许模型修完后重试。"""
        if not self.enabled:
            return
        with self._lock:
            if name == "psp__create_payment_intent":
                checkout_id = arguments.get("checkout_ref")
                state = self._checkouts.get(str(checkout_id)) if isinstance(checkout_id, str) else None
                if state is not None:
                    state.pending_intent = False
            elif name == "psp__capture_payment":
                intent_id = arguments.get("payment_intent_id")
                state = self._state_for_intent(str(intent_id)) if isinstance(intent_id, str) else None
                if state is not None:
                    state.pending_capture = False

    def snapshot(self, checkout_id: str) -> dict[str, Any] | None:
        with self._lock:
            state = self._checkouts.get(checkout_id)
            if state is None:
                return None
            return {
                "checkout_id": state.checkout_id,
                "total": state.total,
                "currency": state.currency,
                "merchant_id": state.merchant_id,
                "sku": state.sku,
                "status": state.status,
                "intent_id": state.intent_id,
                "captured_amount": state.captured_amount,
                "order_id": state.order_id,
                "history": list(state.history),
            }

    def _state_for_intent(self, intent_id: str) -> CheckoutState | None:
        checkout_id = self._intent_to_checkout.get(intent_id)
        return self._checkouts.get(checkout_id) if checkout_id else None
