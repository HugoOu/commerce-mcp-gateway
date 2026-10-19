"""ADR-0029 demo 版：per-call 合法不等于整条交易合法。"""

from __future__ import annotations

import asyncio
import pathlib
import re

from harness import payload, running_gateway
from mcp.client.client import Client


def _text(result: object) -> str:
    return payload(result)[1]


def _num(pattern: str, text: str) -> float:
    found = re.search(pattern, text)
    assert found, text[:300]
    return float(found.group(1))


def _str(pattern: str, text: str) -> str:
    found = re.search(pattern, text)
    assert found, text[:300]
    return found.group(1)


async def _checkout(client: Client, *, quantity: int = 10) -> tuple[str, float]:
    result = await client.call_tool("merchant__create_checkout", {"sku": "taco", "quantity": quantity})
    text = _text(result)
    return _str(r'"checkout_id":\s*"([^"]+)"', text), _num(r'"total":\s*([0-9.]+)', text)


async def _intent(
    client: Client, *, amount: float, checkout_id: str, key: str, currency: str = "CNY"
) -> tuple[str, str]:
    result = await client.call_tool(
        "psp__create_payment_intent",
        {
            "amount": amount,
            "merchant_id": "m1",
            "checkout_ref": checkout_id,
            "currency": currency,
            "idempotency_key": key,
        },
    )
    text = _text(result)
    return _str(r'"payment_intent_id":\s*"([^"]+)"', text), _str(r'"payment_token":\s*"([^"]+)"', text)


async def _capture(client: Client, *, intent: str, amount: float, key: str) -> str:
    result = await client.call_tool(
        "psp__capture_payment",
        {"payment_intent_id": intent, "amount": amount, "idempotency_key": key},
    )
    return _text(result)


async def _complete(client: Client, *, checkout_id: str, token: str) -> str:
    result = await client.call_tool(
        "merchant__complete_checkout", {"checkout_id": checkout_id, "payment_token": token}
    )
    return _text(result)


async def test_normal_purchase_passes(tmp_path: pathlib.Path) -> None:
    async with running_gateway(tmp_path, transaction_guard=True) as (gateway, url):
        async with Client(server=url, mode="auto") as client:
            checkout_id, total = await _checkout(client)
            intent, token = await _intent(client, amount=total, checkout_id=checkout_id, key="normal-1")
            captured = await _capture(client, intent=intent, amount=total, key="normal-1-cap")
            completed = await _complete(client, checkout_id=checkout_id, token=token)
            state = gateway.transactions.snapshot(checkout_id)

    assert '"captured_amount": 43.1' in captured
    assert '"status": "completed"' in completed
    assert state is not None and state["status"] == "completed"


async def test_complete_before_capture_is_denied(tmp_path: pathlib.Path) -> None:
    async with running_gateway(tmp_path, transaction_guard=True) as (gateway, url):
        async with Client(server=url, mode="auto") as client:
            checkout_id, total = await _checkout(client)
            _intent_id, token = await _intent(client, amount=total, checkout_id=checkout_id, key="early-1")
            denied = await _complete(client, checkout_id=checkout_id, token=token)
            state = gateway.transactions.snapshot(checkout_id)

    assert "payment_not_captured" in denied
    assert state is not None and state["status"] == "open"


async def test_forged_token_is_denied(tmp_path: pathlib.Path) -> None:
    async with running_gateway(tmp_path, transaction_guard=True) as (_gateway, url):
        async with Client(server=url, mode="auto") as client:
            checkout_id, total = await _checkout(client)
            intent, _token = await _intent(client, amount=total, checkout_id=checkout_id, key="forge-1")
            await _capture(client, intent=intent, amount=total, key="forge-1-cap")
            denied = await _complete(client, checkout_id=checkout_id, token=f"pt_forged_{checkout_id}")

    assert "token_mismatch" in denied


async def test_under_capture_is_denied(tmp_path: pathlib.Path) -> None:
    async with running_gateway(tmp_path, transaction_guard=True) as (gateway, url):
        async with Client(server=url, mode="auto") as client:
            checkout_id, total = await _checkout(client)
            intent, _token = await _intent(client, amount=total, checkout_id=checkout_id, key="under-1")
            denied = await _capture(client, intent=intent, amount=35.0, key="under-1-cap")
            state = gateway.transactions.snapshot(checkout_id)

    assert "amount_mismatch" in denied
    assert state is not None and state["status"] == "open"


async def test_duplicate_intent_is_denied(tmp_path: pathlib.Path) -> None:
    async with running_gateway(tmp_path, transaction_guard=True) as (_gateway, url):
        async with Client(server=url, mode="auto") as client:
            checkout_id, total = await _checkout(client)
            await _intent(client, amount=total, checkout_id=checkout_id, key="dup-1")
            denied = await client.call_tool(
                "psp__create_payment_intent",
                {
                    "amount": total,
                    "merchant_id": "m1",
                    "checkout_ref": checkout_id,
                    "currency": "CNY",
                    "idempotency_key": "dup-2",
                },
            )

    assert "duplicate_payment_intent" in _text(denied)


async def test_split_transactions_are_denied(tmp_path: pathlib.Path) -> None:
    async with running_gateway(tmp_path, transaction_guard=True) as (_gateway, url):
        async with Client(server=url, mode="auto") as client:
            first_id, first_total = await _checkout(client)
            first_intent, first_token = await _intent(
                client, amount=first_total, checkout_id=first_id, key="split-1"
            )
            await _capture(client, intent=first_intent, amount=first_total, key="split-1-cap")
            await _complete(client, checkout_id=first_id, token=first_token)

            second_id, second_total = await _checkout(client)
            denied = await client.call_tool(
                "psp__create_payment_intent",
                {
                    "amount": second_total,
                    "merchant_id": "m1",
                    "checkout_ref": second_id,
                    "currency": "CNY",
                    "idempotency_key": "split-2",
                },
            )

    assert "mandate_transaction_limit" in _text(denied)


async def test_capture_after_complete_is_denied(tmp_path: pathlib.Path) -> None:
    async with running_gateway(tmp_path, transaction_guard=True) as (_gateway, url):
        async with Client(server=url, mode="auto") as client:
            checkout_id, total = await _checkout(client)
            intent, token = await _intent(client, amount=total, checkout_id=checkout_id, key="after-1")
            await _capture(client, intent=intent, amount=total, key="after-1-cap")
            await _complete(client, checkout_id=checkout_id, token=token)
            denied = await _capture(client, intent=intent, amount=total, key="after-1-cap-2")

    assert "payment_already_captured" in denied


async def test_currency_mismatch_is_denied(tmp_path: pathlib.Path) -> None:
    async with running_gateway(tmp_path, transaction_guard=True) as (_gateway, url):
        async with Client(server=url, mode="auto") as client:
            checkout_id, total = await _checkout(client)
            result = await client.call_tool(
                "psp__create_payment_intent",
                {
                    "amount": total,
                    "merchant_id": "m1",
                    "checkout_ref": checkout_id,
                    "currency": "USD",
                    "idempotency_key": "currency-1",
                },
            )

    assert "currency_mismatch" in _text(result)


async def test_concurrent_duplicate_intent_is_denied(tmp_path: pathlib.Path) -> None:
    """check 与 observe 之间的并发窗口必须被 pending_intent 堵住。"""
    async with running_gateway(tmp_path, transaction_guard=True) as (gateway, url):
        async with Client(server=url, mode="auto") as client:
            checkout_id, total = await _checkout(client)
            results = await asyncio.gather(
                client.call_tool(
                    "psp__create_payment_intent",
                    {
                        "amount": total,
                        "merchant_id": "m1",
                        "checkout_ref": checkout_id,
                        "currency": "CNY",
                        "idempotency_key": "race-a",
                    },
                ),
                client.call_tool(
                    "psp__create_payment_intent",
                    {
                        "amount": total,
                        "merchant_id": "m1",
                        "checkout_ref": checkout_id,
                        "currency": "CNY",
                        "idempotency_key": "race-b",
                    },
                ),
            )
            texts = [_text(result) for result in results]
            state = gateway.transactions.snapshot(checkout_id)

    assert sum("duplicate_payment_intent" in text for text in texts) == 1
    assert state is not None and state["intent_id"]
