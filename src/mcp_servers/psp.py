"""mock PSP MCP（stdio）——业务语义与工具契约见 docs/mock-world.md。

用法：
    uv run python src/mcp_servers/psp.py

环境变量（必须由调用方显式传 env=）：
    PSP_MODE      none | slow | error5xx | decline | insufficient_funds | duplicate_capture | huge_payload
    MOCK_DELAY_S  slow 模式的秒数（默认 3）

幂等语义照 Stripe（ADR-0002 已引用）：同一个 idempotency_key 重放首次响应（失败也重放）；
同 key 不同参数 → 冲突；同一个 intent 用不同 key 再次 capture → 默认拒绝（duplicate_capture 模式除外）。
"""

from __future__ import annotations

import hashlib
import itertools
import json
import uuid
from datetime import datetime, timedelta, timezone

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

try:  # 既能当脚本跑，也能当包导入
    from .common import maybe_crash, maybe_slow, mode
except ImportError:  # pragma: no cover
    from common import maybe_crash, maybe_slow, mode

INTENTS: dict[str, dict] = {}
IDEMPOTENCY: dict[str, tuple[str, dict]] = {}
SPEND_CONTROLS = {
    "per_txn_limit": 50.0,
    "daily_limit": 200.0,
    "allowed_merchants": ["m1"],
    "currency": "CNY",
}
_ref = itertools.count(1)

mcp = MCPServer(name="mock-psp", version="0.1.0")


def _digest(params: dict) -> str:
    payload = json.dumps(params, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _replay(key: str, params: dict) -> dict | None:
    if not key:
        raise ToolError("idempotency_key_required")
    seen = IDEMPOTENCY.get(key)
    if seen is None:
        return None
    stored_digest, response = seen
    if stored_digest != _digest(params):
        raise ToolError("idempotency_key_reused_with_different_parameters")
    return dict(response)


def _remember(key: str, params: dict, response: dict) -> None:
    IDEMPOTENCY[key] = (_digest(params), dict(response))


def _money(value: float) -> float:
    return round(value + 1e-9, 2)


def _spent_today() -> float:
    return _money(sum(row["amount"] for row in INTENTS.values() if row["status"] == "succeeded"))


@mcp.tool()
def create_payment_intent(amount: float, merchant_id: str, checkout_ref: str, currency: str = "CNY",
                          mandate_ref: str = "", idempotency_key: str = "") -> dict:
    """Authorize (hold) an amount for a checkout; capture happens separately (Stripe manual capture)."""
    params = {"op": "create_payment_intent", "amount": amount, "merchant_id": merchant_id,
              "checkout_ref": checkout_ref, "currency": currency}
    replayed = _replay(idempotency_key, params)
    if replayed is not None:
        return replayed
    current = mode("PSP_MODE")
    maybe_slow(current)
    maybe_crash(current)
    if amount <= 0:
        raise ToolError("invalid_amount: must be > 0")
    intent_id = f"pi_{next(_ref):04d}{uuid.uuid4().hex[:6]}"
    intent = {
        "payment_intent_id": intent_id,
        "status": "requires_capture",
        "amount": _money(amount),
        "currency": currency,
        "merchant_id": merchant_id,
        "checkout_ref": checkout_ref,
        "mandate_ref": mandate_ref,
        "payment_token": f"pt_{intent_id}_{checkout_ref}",
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(timespec="seconds"),
    }
    if current in ("decline", "insufficient_funds"):
        # 业务失败不是工具失败：Stripe 侧是「调用成功 + intent 状态为失败」
        intent["status"] = "requires_payment_method"
        intent["last_payment_error"] = {
            "code": "card_declined" if current == "decline" else "insufficient_funds",
            "message": "authorization was not granted by the issuer",
        }
        intent.pop("payment_token", None)
    if current == "huge_payload":
        intent["debug_blob"] = "x" * 200_000
    INTENTS[intent_id] = intent
    _remember(idempotency_key, params, intent)
    return dict(intent)


@mcp.tool()
def capture_payment(payment_intent_id: str, idempotency_key: str = "", amount: float = 0.0) -> dict:
    """Capture (charge) an authorized intent."""
    params = {"op": "capture_payment", "payment_intent_id": payment_intent_id, "amount": amount}
    replayed = _replay(idempotency_key, params)
    if replayed is not None:
        return replayed
    current = mode("PSP_MODE")
    maybe_slow(current)
    maybe_crash(current)
    intent = INTENTS.get(payment_intent_id)
    if intent is None:
        raise ToolError(f"unknown_payment_intent: {payment_intent_id}")
    if intent["status"] == "succeeded":
        if current == "duplicate_capture":
            intent["captured_amount"] = _money(intent["captured_amount"] + intent["amount"])
            intent["double_charged"] = True
        else:
            raise ToolError(f"payment_intent_already_captured: {payment_intent_id}")
    elif intent["status"] != "requires_capture":
        raise ToolError(f"payment_intent_not_capturable: {payment_intent_id} is {intent['status']}")
    captured = _money(amount or intent["amount"])
    if captured > intent["amount"]:
        raise ToolError("capture_amount_exceeds_authorized")
    intent["status"] = "succeeded"
    intent["captured_amount"] = _money(intent.get("captured_amount", 0.0) + captured)
    intent["receipt_id"] = f"rcpt_{uuid.uuid4().hex[:10]}"
    _remember(idempotency_key, params, intent)
    return {"payment_intent_id": payment_intent_id, "status": "succeeded",
            "captured_amount": intent["captured_amount"], "receipt_id": intent["receipt_id"]}


@mcp.tool()
def cancel_payment_intent(payment_intent_id: str, idempotency_key: str = "") -> dict:
    """Release a hold without charging."""
    params = {"op": "cancel_payment_intent", "payment_intent_id": payment_intent_id}
    replayed = _replay(idempotency_key, params)
    if replayed is not None:
        return replayed
    intent = INTENTS.get(payment_intent_id)
    if intent is None:
        raise ToolError(f"unknown_payment_intent: {payment_intent_id}")
    if intent["status"] == "succeeded":
        raise ToolError(f"payment_intent_already_captured: {payment_intent_id}")
    intent["status"] = "canceled"
    _remember(idempotency_key, params, intent)
    return {"payment_intent_id": payment_intent_id, "status": "canceled"}


@mcp.tool()
def refund_payment(payment_intent_id: str, reason: str = "customer_request", amount: float = 0.0,
                   idempotency_key: str = "") -> dict:
    """Refund a captured payment."""
    params = {"op": "refund_payment", "payment_intent_id": payment_intent_id, "amount": amount,
              "reason": reason}
    replayed = _replay(idempotency_key, params)
    if replayed is not None:
        return replayed
    intent = INTENTS.get(payment_intent_id)
    if intent is None:
        raise ToolError(f"unknown_payment_intent: {payment_intent_id}")
    if intent["status"] != "succeeded":
        raise ToolError(f"payment_intent_not_refundable: {payment_intent_id} is {intent['status']}")
    refund_amount = _money(amount or intent.get("captured_amount", intent["amount"]))
    if refund_amount > intent.get("captured_amount", intent["amount"]):
        raise ToolError("refund_amount_exceeds_captured")
    refund = {"refund_id": f"rfd_{uuid.uuid4().hex[:10]}", "payment_intent_id": payment_intent_id,
              "status": "refunded", "amount": refund_amount, "reason": reason}
    _remember(idempotency_key, params, refund)
    return refund


@mcp.tool()
def get_payment(payment_intent_id: str) -> dict:
    """Look up the current state of an intent (reconciliation)."""
    intent = INTENTS.get(payment_intent_id)
    if intent is None:
        raise ToolError(f"unknown_payment_intent: {payment_intent_id}")
    return dict(intent)


@mcp.tool()
def get_spend_controls(mandate_ref: str = "") -> dict:
    """Issuer / network side spend controls — an external ground truth for the gateway ledger."""
    return {**SPEND_CONTROLS, "mandate_ref": mandate_ref, "spent_today": _spent_today(),
            "captured_total": _spent_today()}


if __name__ == "__main__":
    mcp.run(transport="stdio")
