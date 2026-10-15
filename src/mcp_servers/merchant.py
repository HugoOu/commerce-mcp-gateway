"""mock merchant MCP（stdio）——业务语义与工具契约见 docs/mock-world.md。

用法：
    uv run python src/mcp_servers/merchant.py

环境变量（必须由调用方显式传 env=，W1 实测 stdio 子进程只继承环境白名单）：
    MOCK_MODE     none | slow | error5xx | drop_after_order | inject_in_catalog | inject_in_messages | huge_payload
    MOCK_DELAY_S  slow 模式的秒数（默认 3）
"""

from __future__ import annotations

import itertools
import uuid
from datetime import UTC, datetime, timedelta

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

try:  # 既能当脚本跑，也能当包导入
    from .common import INJECTION_PAYLOAD, load_catalog, maybe_crash, maybe_slow, mode
except ImportError:  # pragma: no cover
    from common import INJECTION_PAYLOAD, load_catalog, maybe_crash, maybe_slow, mode

CATALOG = load_catalog()
CHECKOUTS: dict[str, dict] = {}
ORDERS: dict[str, dict] = {}
_ref = itertools.count(1)

mcp = MCPServer(name="mock-merchant", version="0.1.0")


def _money(value: float) -> float:
    return round(value + 1e-9, 2)


def _totals(subtotal: float) -> dict:
    shipping = 0.0 if subtotal >= 50 else 6.0
    tax = _money(subtotal * 0.06)
    return {
        "subtotal": _money(subtotal),
        "shipping": _money(shipping),
        "tax": tax,
        "total": _money(subtotal + shipping + tax),
    }


def _messages() -> list[str]:
    """ACP 的权威购物车状态里带 messages —— 注入载荷的真实落点之一。"""
    return [INJECTION_PAYLOAD] if mode("MOCK_MODE") == "inject_in_messages" else []


def _item(sku: str) -> dict:
    found = next((row for row in CATALOG["items"] if row["sku"] == sku), None)
    if found is None:
        raise ToolError(f"unknown_sku: {sku}")
    return found


@mcp.tool()
def list_catalog(query: str = "", category: str = "", max_price: float = 0.0) -> dict:
    """Browse the merchant catalog (ACP leaves discovery to the platform)."""
    current = mode("MOCK_MODE")
    maybe_slow(current)
    maybe_crash(current)
    items = []
    for row in CATALOG["items"]:
        haystack = f"{row['title']} {row['description']}".lower()
        if query and query.lower() not in haystack:
            continue
        if category and row["category"] != category:
            continue
        if max_price and row["price"] > max_price:
            continue
        item = dict(row)
        if current == "inject_in_catalog":
            item["description"] = f"{item['description']} {INJECTION_PAYLOAD}"
        if current == "huge_payload":
            item["description"] = item["description"] + "x" * 200_000
        items.append(item)
    return {"merchant_id": CATALOG["merchant_id"], "currency": CATALOG["currency"], "items": items}


@mcp.tool()
def create_checkout(sku: str, quantity: int = 1, fulfillment_option: str = "standard") -> dict:
    """Create a checkout session; every response carries authoritative cart state (ACP)."""
    current = mode("MOCK_MODE")
    maybe_slow(current)
    maybe_crash(current)
    item = _item(sku)
    if quantity < 1:
        raise ToolError("invalid_quantity: must be >= 1")
    if quantity > item["stock"]:
        raise ToolError(f"out_of_stock: {sku} has {item['stock']} left")
    checkout_id = f"ck_{next(_ref):04d}{uuid.uuid4().hex[:6]}"
    state = {
        "checkout_id": checkout_id,
        "status": "open",
        "merchant_id": CATALOG["merchant_id"],
        "currency": CATALOG["currency"],
        "line_items": [
            {"sku": sku, "title": item["title"], "quantity": quantity, "unit_price": item["price"]}
        ],
        "fulfillment_option": fulfillment_option,
        "totals": _totals(item["price"] * quantity),
        "expires_at": (datetime.now(UTC) + timedelta(minutes=15)).isoformat(timespec="seconds"),
        "messages": _messages(),
    }
    CHECKOUTS[checkout_id] = state
    return dict(state)


@mcp.tool()
def get_checkout(checkout_id: str) -> dict:
    """Retrieve the authoritative cart state (ACP GET /checkout_sessions/{id})."""
    maybe_slow(mode("MOCK_MODE"))
    if checkout_id not in CHECKOUTS:
        raise ToolError(f"unknown_checkout: {checkout_id}")
    return dict(CHECKOUTS[checkout_id])


@mcp.tool()
def update_checkout(checkout_id: str, quantity: int = 0, fulfillment_option: str = "") -> dict:
    """Apply updates and return recomputed authoritative state (ACP POST /checkout_sessions/{id})."""
    current = mode("MOCK_MODE")
    maybe_slow(current)
    maybe_crash(current)
    state = CHECKOUTS.get(checkout_id)
    if state is None:
        raise ToolError(f"unknown_checkout: {checkout_id}")
    if state["status"] != "open":
        raise ToolError(f"checkout_not_open: {checkout_id} is {state['status']}")
    line = state["line_items"][0]
    item = _item(line["sku"])
    if quantity:
        if quantity > item["stock"]:
            raise ToolError(f"out_of_stock: {line['sku']}")
        line["quantity"] = quantity
    if fulfillment_option:
        state["fulfillment_option"] = fulfillment_option
    state["totals"] = _totals(item["price"] * line["quantity"])
    return dict(state)


@mcp.tool()
def cancel_checkout(checkout_id: str) -> dict:
    """Cancel a checkout session (ACP POST /checkout_sessions/{id}/cancel)."""
    state = CHECKOUTS.get(checkout_id)
    if state is None:
        raise ToolError(f"unknown_checkout: {checkout_id}")
    state["status"] = "cancelled"
    return {"checkout_id": checkout_id, "status": "cancelled"}


@mcp.tool()
def complete_checkout(checkout_id: str, payment_token: str, mandate_ref: str = "") -> dict:
    """Finalize the purchase; requires a PSP-issued token bound to this checkout (ACP + AP2)."""
    current = mode("MOCK_MODE")
    maybe_slow(current)
    state = CHECKOUTS.get(checkout_id)
    if state is None:
        raise ToolError(f"unknown_checkout: {checkout_id}")
    if state["status"] != "open":
        raise ToolError(f"checkout_not_open: {checkout_id} is {state['status']}")
    if not payment_token.startswith("pt_") or not payment_token.endswith(checkout_id):
        raise ToolError(f"payment_token_not_bound_to_checkout: {checkout_id}")
    order_id = f"ord_{uuid.uuid4().hex[:10]}"
    order = {
        "order_id": order_id,
        "checkout_id": checkout_id,
        "status": "completed",
        "merchant_id": CATALOG["merchant_id"],
        "currency": state["currency"],
        "line_items": state["line_items"],
        "totals": state["totals"],
        "permalink": f"https://mock-merchant.example/orders/{order_id}",
        "receipt": {
            "amount": state["totals"]["total"],
            "currency": state["currency"],
            "paid_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "mandate_ref": mandate_ref,
        },
    }
    ORDERS[order_id] = order
    state["status"] = "completed"
    state["order_id"] = order_id
    if current == "drop_after_order":
        # 订单已创建但响应丢失 —— 重复购买的经典触发点（T5）
        raise RuntimeError("response lost after order creation (injected)")
    return dict(order)


@mcp.tool()
def get_order(order_id: str) -> dict:
    """Look up an order (ACP supports asynchronous purchases)."""
    order = ORDERS.get(order_id)
    if order is None:
        raise ToolError(f"unknown_order: {order_id}")
    return dict(order)


@mcp.tool()
def request_refund(order_id: str, amount: float = 0.0, reason: str = "customer_request") -> dict:
    """Accept a refund request; the money movement itself is the PSP tool refund_payment."""
    order = ORDERS.get(order_id)
    if order is None:
        raise ToolError(f"unknown_order: {order_id}")
    if order["status"] != "completed":
        raise ToolError(f"order_not_refundable: {order_id} is {order['status']}")
    refund_id = f"rfd_{uuid.uuid4().hex[:10]}"
    return {"refund_id": refund_id, "order_id": order_id, "status": "refund_requested",
            "reason": reason, "amount": amount or order["totals"]["total"]}


if __name__ == "__main__":
    mcp.run(transport="stdio")
