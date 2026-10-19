"""I5 的持久化边界：交易账本是进程内的，重启/多副本会丢状态。

两个场景：

1. `restart_then_second_purchase`：第一笔完成 → 关网关 → 第二笔；
   预算 SQLite 还在，但 `max_transactions=1` 的计数归零，第二笔会被放行。
2. `restart_between_capture_and_complete`：capture 后关网关 → 新网关 complete；
   交易状态与 mock merchant 的 checkout 都是进程内状态，合法订单会完不成。

用法：`uv run python eval/persistence_probe.py --runs 10`。
"""

from __future__ import annotations

import argparse
import asyncio
import pathlib
import re
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from harness import payload, running_gateway
from mcp.client.client import Client


def _text(result: object) -> str:
    return payload(result)[1]


async def _checkout(client: Client) -> tuple[str, float]:
    result = await client.call_tool("merchant__create_checkout", {"sku": "taco", "quantity": 10})
    text = _text(result)
    checkout = re.search(r'"checkout_id":\s*"([^"]+)"', text)
    total = re.search(r'"total":\s*([0-9.]+)', text)
    if not checkout or not total:
        raise RuntimeError(text[:300])
    return checkout.group(1), float(total.group(1))


async def _intent(client: Client, *, checkout: str, amount: float, key: str) -> tuple[str, str]:
    result = await client.call_tool(
        "psp__create_payment_intent",
        {
            "amount": amount,
            "merchant_id": "m1",
            "checkout_ref": checkout,
            "currency": "CNY",
            "idempotency_key": key,
        },
    )
    text = _text(result)
    intent = re.search(r'"payment_intent_id":\s*"([^"]+)"', text)
    token = re.search(r'"payment_token":\s*"([^"]+)"', text)
    if not intent or not token:
        raise RuntimeError(text[:300])
    return intent.group(1), token.group(1)


async def _capture(client: Client, *, intent: str, amount: float, key: str) -> str:
    result = await client.call_tool(
        "psp__capture_payment",
        {"payment_intent_id": intent, "amount": amount, "idempotency_key": key},
    )
    return _text(result)


async def _complete(client: Client, *, checkout: str, token: str) -> str:
    result = await client.call_tool(
        "merchant__complete_checkout", {"checkout_id": checkout, "payment_token": token}
    )
    return _text(result)


async def _purchase(client: Client, key: str) -> dict[str, object]:
    checkout, total = await _checkout(client)
    intent, token = await _intent(client, checkout=checkout, amount=total, key=f"{key}-auth")
    captured = await _capture(client, intent=intent, amount=total, key=f"{key}-cap")
    completed = await _complete(client, checkout=checkout, token=token)
    return {
        "checkout": checkout,
        "total": total,
        "token": token,
        "captured": '"status": "succeeded"' in captured,
        "completed": '"status": "completed"' in completed,
        "complete_text": completed,
    }


async def _restart_second_purchase(tmp_path: pathlib.Path) -> tuple[bool, float]:
    async with running_gateway(tmp_path, transaction_guard=True) as (_gateway, url), Client(
        server=url, mode="auto"
    ) as client:
        first = await _purchase(client, "first")
    async with running_gateway(tmp_path, transaction_guard=True) as (gateway, url), Client(
        server=url, mode="auto"
    ) as client:
        second = await _purchase(client, "second")
        committed = gateway.budget.balance("agent-dev").committed
    return bool(first["completed"] and second["completed"]), committed


async def _restart_between_capture(tmp_path: pathlib.Path) -> tuple[bool, str]:
    async with running_gateway(tmp_path, transaction_guard=True) as (_gateway, url), Client(
        server=url, mode="auto"
    ) as client:
        checkout, total = await _checkout(client)
        intent, token = await _intent(client, checkout=checkout, amount=total, key="split-auth")
        await _capture(client, intent=intent, amount=total, key="split-cap")
    async with running_gateway(tmp_path, transaction_guard=True) as (_gateway, url), Client(
        server=url, mode="auto"
    ) as client:
        text = await _complete(client, checkout=checkout, token=token)
    return '"status": "completed"' in text, text


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=10)
    args = parser.parse_args()
    second_ok = 0
    committed_total = 0.0
    denied = 0
    for _ in range(args.runs):
        with tempfile.TemporaryDirectory(prefix="persist-a-") as tmp:
            ok, committed = await _restart_second_purchase(pathlib.Path(tmp))
            second_ok += 1 if ok else 0
            committed_total += committed
        with tempfile.TemporaryDirectory(prefix="persist-b-") as tmp:
            completed, _text = await _restart_between_capture(pathlib.Path(tmp))
            denied += 1 if not completed else 0

    print(f"N={args.runs}")
    print(f"restart_second_purchase_allowed={second_ok}/{args.runs}")
    print(f"mean_committed_after_two={committed_total / args.runs:.2f}")
    print(f"restart_between_capture_complete_denied={denied}/{args.runs}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
