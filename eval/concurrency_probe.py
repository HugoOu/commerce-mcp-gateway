"""I5 的并发边界：同一 checkout 上两个并发 create_payment_intent。

TransactionGuard 的 check 与 observe 是分开的：两个并发请求可能都通过 check，
都到达 PSP；第二个 observe 会被忽略，留下一个账本未跟踪的 intent。

本探针不依赖模型；N=30 个 trial，每个 trial 新建一个 checkout，然后同时发两个
intent（不同 idempotency key）。报告 duplicate intent 率、孤儿 intent 数、
孤儿 intent 是否可被 capture、以及是否产生实际扣款。

用法：`uv run python eval/concurrency_probe.py --runs 30`。
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
sys.path.insert(0, str(ROOT / "eval"))

import yaml
from harness import payload, running_gateway
from mcp.client.client import Client
from stats import clopper_pearson_upper


def _mandate(path: pathlib.Path) -> None:
    path.write_text(
        yaml.safe_dump(
            {
                "mandate_id": "mnd_concurrency",
                "kind": "open",
                "currency": "CNY",
                "budget_total": 100.0,
                "per_txn_max": 50.0,
                "max_transactions": 100,
                "allowed_merchants": ["m1"],
                "allowed_categories": ["food", "drink"],
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )


async def _checkout(client: Client) -> tuple[str, float]:
    result = await client.call_tool("merchant__create_checkout", {"sku": "taco", "quantity": 10})
    text = payload(result)[1]
    checkout = re.search(r'"checkout_id":\s*"([^"]+)"', text)
    total = re.search(r'"total":\s*([0-9.]+)', text)
    if not checkout or not total:
        raise RuntimeError(text[:300])
    return checkout.group(1), float(total.group(1))


async def _intent(client: Client, *, checkout: str, amount: float, key: str) -> tuple[bool, str]:
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
    is_error, text = payload(result)
    found = re.search(r'"payment_intent_id":\s*"([^"]+)"', text)
    return (not is_error and found is not None), found.group(1) if found else ""


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=30)
    args = parser.parse_args()

    duplicate_trials = guard_denied = orphan_count = orphan_capturable = 0
    with tempfile.TemporaryDirectory(prefix="concurrency-") as tmp:
        tmp_path = pathlib.Path(tmp)
        mandate = tmp_path / "mandate.yaml"
        _mandate(mandate)
        async with running_gateway(
            tmp_path, transaction_guard=True, mandate_path=mandate
        ) as (gateway, url):
            async with Client(server=url, mode="auto") as client:
                for index in range(1, args.runs + 1):
                    checkout, total = await _checkout(client)
                    results = await asyncio.gather(
                        _intent(client, checkout=checkout, amount=total, key=f"race-{index}-a"),
                        _intent(client, checkout=checkout, amount=total, key=f"race-{index}-b"),
                    )
                    successes = [intent for ok, intent in results if ok]
                    if len(set(successes)) >= 2:
                        duplicate_trials += 1
                    if len(successes) == 1 and any(not ok for ok, _ in results):
                        guard_denied += 1

                    state = gateway.transactions.snapshot(checkout)
                    tracked = state["intent_id"] if state else None
                    orphans = [intent for intent in successes if intent != tracked]
                    orphan_count += len(orphans)
                    if orphans:
                        captured = await client.call_tool(
                            "psp__capture_payment",
                            {
                                "payment_intent_id": orphans[0],
                                "amount": total,
                                "idempotency_key": f"race-{index}-orphan-cap",
                            },
                        )
                        if not payload(captured)[0]:
                            orphan_capturable += 1

            committed = gateway.budget.balance("agent-dev").committed

    print(f"N={args.runs} trials")
    print(f"duplicate_intent_trials={duplicate_trials}")
    print(f"guard_serialized_trials={guard_denied}")
    print(f"orphan_intents={orphan_count}")
    print(f"orphan_capturable={orphan_capturable}")
    print(f"committed={committed:.2f}")
    print(f"orphan_capture_rate_upper={clopper_pearson_upper(orphan_capturable, args.runs):.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
