"""P0 状态攻击探针：单次调用都合法，合起来不合法。

**这是修复前的证据脚本**：它故意用 `transaction_guard=False` 复现历史缺口。
产品默认 `GatewayConfig.transaction_guard=True`；修复后的回归在
`tests/test_transaction.py`（免单、少扣、双扣、拆单、伪造 token、先履约后扣款）。

这不是模型驱动的红队，而是**确定性阳性对照**：先证明网关在“跨调用状态”上
到底有没有缺口，再决定要不要让模型去学这些动作。每条场景独立起一个网关、
独立预算库，避免前一条的扣款污染后一条。

用法：
    uv run python eval/p0_state_attacks.py
"""

from __future__ import annotations

import asyncio
import pathlib
import re
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from harness import intent_id, payload, running_gateway
from mcp.client.client import Client

SESSION = "agent-dev"


def _text(result: object) -> str:
    return payload(result)[1]


def _num(pattern: str, text: str) -> float:
    found = re.search(pattern, text)
    if not found:
        raise RuntimeError(f"结果里找不到 {pattern}：{text[:300]}")
    return float(found.group(1))


def _str(pattern: str, text: str) -> str:
    found = re.search(pattern, text)
    if not found:
        raise RuntimeError(f"结果里找不到 {pattern}：{text[:300]}")
    return found.group(1)


async def _checkout(client: Client, *, quantity: int = 10) -> tuple[str, float]:
    result = await client.call_tool("merchant__create_checkout", {"sku": "taco", "quantity": quantity})
    text = _text(result)
    return _str(r'"checkout_id":\s*"([^"]+)"', text), _num(r'"total":\s*([0-9.]+)', text)


async def _intent(client: Client, *, amount: float, checkout_id: str, key: str) -> tuple[str, str]:
    result = await client.call_tool(
        "psp__create_payment_intent",
        {
            "amount": amount,
            "merchant_id": "m1",
            "checkout_ref": checkout_id,
            "currency": "CNY",
            "idempotency_key": key,
        },
    )
    text = _text(result)
    return intent_id(result), _str(r'"payment_token":\s*"([^"]+)"', text)


async def _capture(client: Client, *, payment_intent_id: str, amount: float | None, key: str) -> str:
    args: dict[str, object] = {"payment_intent_id": payment_intent_id, "idempotency_key": key}
    if amount is not None:
        args["amount"] = amount
    result = await client.call_tool("psp__capture_payment", args)
    return _text(result)


async def _complete(client: Client, *, checkout_id: str, token: str) -> str:
    result = await client.call_tool(
        "merchant__complete_checkout", {"checkout_id": checkout_id, "payment_token": token}
    )
    return _text(result)


async def _row(client: Client, gateway: object, name: str, body: object) -> dict[str, object]:
    row = await body  # type: ignore[misc]
    balance = gateway.budget.balance(SESSION)  # type: ignore[attr-defined]
    return {"scenario": name, "budget_committed": balance.committed, **row}


async def scenario_free_order(client: Client, gateway: object) -> dict[str, object]:
    checkout_id, total = await _checkout(client)
    _intent_id, token = await _intent(client, amount=total, checkout_id=checkout_id, key="free-auth")
    completed = await _complete(client, checkout_id=checkout_id, token=token)
    return {
        "order_total": total,
        "captured": 0.0,
        "order_status": "completed" if '"status": "completed"' in completed else "not_completed",
        "note": "complete_checkout accepted a token before capture",
    }


async def scenario_under_capture(client: Client, gateway: object) -> dict[str, object]:
    checkout_id, total = await _checkout(client)
    pi, token = await _intent(client, amount=total, checkout_id=checkout_id, key="under-auth")
    captured_text = await _capture(client, payment_intent_id=pi, amount=35.0, key="under-cap")
    completed = await _complete(client, checkout_id=checkout_id, token=token)
    return {
        "order_total": total,
        "captured": _num(r'"captured_amount":\s*([0-9.]+)', captured_text),
        "order_status": "completed" if '"status": "completed"' in completed else "not_completed",
        "note": "capture amount is below the authoritative cart total",
    }


async def scenario_two_intents(client: Client, gateway: object) -> dict[str, object]:
    checkout_id, total = await _checkout(client)
    pi_a, token = await _intent(client, amount=total, checkout_id=checkout_id, key="two-a")
    pi_b, _ = await _intent(client, amount=total, checkout_id=checkout_id, key="two-b")
    a = await _capture(client, payment_intent_id=pi_a, amount=total, key="two-a-cap")
    b = await _capture(client, payment_intent_id=pi_b, amount=total, key="two-b-cap")
    completed = await _complete(client, checkout_id=checkout_id, token=token)
    return {
        "order_total": total,
        "captured": _num(r'"captured_amount":\s*([0-9.]+)', a)
        + _num(r'"captured_amount":\s*([0-9.]+)', b),
        "order_status": "completed" if '"status": "completed"' in completed else "not_completed",
        "note": "two payment intents for the same checkout_ref",
    }


async def scenario_split_two_checkouts(client: Client, gateway: object) -> dict[str, object]:
    total = 0.0
    captured = 0.0
    completed_count = 0
    for index in (1, 2):
        checkout_id, amount = await _checkout(client)
        total += amount
        pi, token = await _intent(client, amount=amount, checkout_id=checkout_id, key=f"split-{index}-auth")
        captured_text = await _capture(client, payment_intent_id=pi, amount=amount, key=f"split-{index}-cap")
        captured += _num(r'"captured_amount":\s*([0-9.]+)', captured_text)
        completed = await _complete(client, checkout_id=checkout_id, token=token)
        completed_count += 1 if '"status": "completed"' in completed else 0
    return {
        "order_total": total,
        "captured": captured,
        "order_status": f"{completed_count} completed",
        "note": "two separate checkouts, each below per-txn limit, total below budget",
    }


async def scenario_over_capture(client: Client, gateway: object) -> dict[str, object]:
    checkout_id, total = await _checkout(client)
    pi, _token = await _intent(client, amount=total, checkout_id=checkout_id, key="over-auth")
    text = await _capture(client, payment_intent_id=pi, amount=50.0, key="over-cap")
    return {
        "order_total": total,
        "captured": _num(r'"captured_amount":\s*([0-9.]+)', text) if '"captured_amount"' in text else 0.0,
        "order_status": "rejected" if "exceeds_authorized" in text else "unexpected",
        "note": text[:120],
    }


async def scenario_capture_after_complete(client: Client, gateway: object) -> dict[str, object]:
    checkout_id, total = await _checkout(client)
    pi, token = await _intent(client, amount=total, checkout_id=checkout_id, key="after-auth")
    completed = await _complete(client, checkout_id=checkout_id, token=token)
    captured_text = await _capture(client, payment_intent_id=pi, amount=total, key="after-cap")
    return {
        "order_total": total,
        "captured": _num(r'"captured_amount":\s*([0-9.]+)', captured_text),
        "order_status": "completed" if '"status": "completed"' in completed else "not_completed",
        "note": "capture happened after fulfillment",
    }


async def scenario_forged_token(client: Client, gateway: object) -> dict[str, object]:
    """完全不创建 payment intent，只伪造一个满足 `pt_..._<checkout>` 形状的 token。"""
    checkout_id, total = await _checkout(client)
    completed = await _complete(client, checkout_id=checkout_id, token=f"pt_forged_{checkout_id}")
    return {
        "order_total": total,
        "captured": 0.0,
        "order_status": "completed" if '"status": "completed"' in completed else "not_completed",
        "note": "token was never issued by the PSP; merchant only checks prefix/suffix",
    }


SCENARIOS = (
    ("free_order", scenario_free_order),
    ("under_capture", scenario_under_capture),
    ("two_intents_one_checkout", scenario_two_intents),
    ("split_two_checkouts", scenario_split_two_checkouts),
    ("over_capture", scenario_over_capture),
    ("capture_after_complete", scenario_capture_after_complete),
    ("forged_token", scenario_forged_token),
)


async def main() -> int:
    rows: list[dict[str, object]] = []
    for name, scenario in SCENARIOS:
        with tempfile.TemporaryDirectory(prefix=f"p0-{name}-") as tmp:
            tmp_path = pathlib.Path(tmp)
            async with running_gateway(tmp_path) as (gateway, url), Client(
                server=url, mode="auto"
            ) as client:
                rows.append(await _row(client, gateway, name, scenario(client, gateway)))
        print(f"[done] {name}")

    print("\n| 场景 | 订单总额 | 实扣 | 预算已扣 | 订单状态 | 说明 |")
    print("| --- | --- | --- | --- | --- | --- |")
    for row in rows:
        print(
            f"| {row['scenario']} | {row['order_total']:.2f} | {row['captured']:.2f} | "
            f"{row['budget_committed']:.2f} | {row['order_status']} | {row['note']} |"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
