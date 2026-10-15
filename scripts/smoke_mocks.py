"""Step 1 验收脚本：一条命令把两个 mock 与全部故障注入开关各跑一遍。

用法（项目根目录）：
    uv run python scripts/smoke_mocks.py
    uv run python scripts/smoke_mocks.py --only merchant
    uv run python scripts/smoke_mocks.py --only modes

为什么要有它：`docs/w2-plan.md` 的 Step 1 验收要求「两个 mock 都能被调通，且每个故障模式
都能用一条命令复现」。这个脚本就是那条命令。

两点实现说明：
  1. 每次检查都新起一个 mock 子进程（模式写在 env 里），所以 MOCK_MODE 的生效本身也在被验证
     —— W1 实测 stdio 子进程只继承环境白名单，配置必须显式传 env=。
  2. mock 的工具返回 dict，SDK 默认**不填** structuredContent（W1 实测），所以结果体是
     content 里的 JSON 文本；本脚本按这个形态解析，不假设 structuredContent 存在。
"""

import argparse
import asyncio
import json
import pathlib
import sys
from typing import Any

from mcp.client.client import Client
from mcp.client.stdio import StdioServerParameters

ROOT = pathlib.Path(__file__).resolve().parents[1]
MERCHANT = ROOT / "src" / "mcp_servers" / "merchant.py"
PSP = ROOT / "src" / "mcp_servers" / "psp.py"

RESULTS: list[tuple[str, bool, str]] = []


def leaves(exc: BaseException):
    if isinstance(exc, BaseExceptionGroup):
        for inner in exc.exceptions:
            yield from leaves(inner)
    else:
        yield exc


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(("[PASS] " if ok else "[FAIL] ") + name + ((" -- " + detail) if detail else ""))


def target(path: pathlib.Path, env_name: str, mode: str) -> StdioServerParameters:
    """显式传 env：stdio 子进程只继承白名单，不继承我们的 shell 环境。"""
    return StdioServerParameters(
        command=sys.executable,
        args=[str(path)],
        env={env_name: mode, "MOCK_DELAY_S": "0.2"},
    )


async def call(client: Client, name: str, arguments: dict) -> tuple[bool, str, dict]:
    """调用工具，摊平成 (isError, 载荷文本, 解析后的对象)——失败也是可观察的返回值。"""
    result = await client.call_tool(name, arguments)
    data = result.model_dump(by_alias=True)
    texts = [block.get("text", "") for block in data.get("content", []) if isinstance(block, dict)]
    text = " | ".join(texts)
    obj: dict[str, Any] = {}
    if not data.get("isError") and text.strip().startswith("{"):
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            obj = {}
    return bool(data.get("isError")), text, obj


async def check_merchant() -> None:
    async with Client(server=target(MERCHANT, "MOCK_MODE", "none"), mode="auto") as client:
        tools = [tool.name for tool in (await client.list_tools()).tools]
        check("merchant: tools/list 有 8 个工具", len(tools) == 8, ", ".join(tools))

        err, text, obj = await call(client, "list_catalog", {"category": "food"})
        check("merchant: list_catalog(category=food) 返回 2 件",
              not err and len(obj.get("items", [])) == 2, f"isError={err} {text[:60]}")

        err, text, state = await call(client, "create_checkout", {"sku": "burrito", "quantity": 1})
        totals = state.get("totals", {})
        check("merchant: create_checkout 返回权威购物车状态",
              not err and state.get("status") == "open" and totals.get("total") == 50.52,
              f"totals={totals}")

        checkout_id = state.get("checkout_id", "")
        err, text, _ = await call(client, "complete_checkout",
                                  {"checkout_id": checkout_id, "payment_token": "bogus"})
        check("merchant: 支付令牌未绑定到该 checkout 必须拒绝",
              err and "payment_token_not_bound" in text, text[:80])

        err, text, _ = await call(client, "get_order", {"order_id": "ord_missing"})
        check("merchant: 未知订单是可预期失败（模型看得到）", err and "unknown_order" in text, text[:80])


async def check_psp() -> None:
    async with Client(server=target(PSP, "PSP_MODE", "none"), mode="auto") as client:
        tools = [tool.name for tool in (await client.list_tools()).tools]
        check("psp: tools/list 有 6 个工具", len(tools) == 6, ", ".join(tools))

        args = {"amount": 50.52, "merchant_id": "m1", "checkout_ref": "ck_smoke", "idempotency_key": "idem-1"}
        err, text, intent = await call(client, "create_payment_intent", args)
        intent_id = intent.get("payment_intent_id", "")
        check("psp: 授权成功（requires_capture + 支付令牌）",
              not err and intent.get("status") == "requires_capture" and bool(intent.get("payment_token")),
              f"status={intent.get(chr(115)+chr(116)+chr(97)+chr(116)+chr(117)+chr(115))}")

        err, _, replayed = await call(client, "create_payment_intent", args)
        check("psp: 同 key 重放首次响应（Stripe 语义）",
              not err and replayed.get("payment_intent_id") == intent_id,
              f"{intent_id} -> {replayed.get(chr(112)+chr(97)+chr(121)+chr(109)+chr(101)+chr(110)+chr(116)+chr(95)+chr(105)+chr(110)+chr(116)+chr(101)+chr(110)+chr(116)+chr(95)+chr(105)+chr(100))}")

        err, text, _ = await call(client, "create_payment_intent", {**args, "amount": 9.99})
        check("psp: 同 key 不同参数必须冲突", err and "different_parameters" in text, text[:80])

        err, text, captured = await call(client, "capture_payment",
                                        {"payment_intent_id": intent_id, "idempotency_key": "idem-2"})
        check("psp: 捕获成功（succeeded）",
              not err and captured.get("status") == "succeeded", str(captured.get("captured_amount")))

        err, text, _ = await call(client, "capture_payment",
                                  {"payment_intent_id": intent_id, "idempotency_key": "idem-3"})
        check("psp: 不同 key 重复捕获必须被 PSP 拒绝", err and "already_captured" in text, text[:80])

        err, _, controls = await call(client, "get_spend_controls", {})
        check("psp: 外部 ground truth（spend controls）可读",
              not err and controls.get("spent_today") == 50.52,
              f"spent_today={controls.get(chr(115)+chr(112)+chr(101)+chr(110)+chr(116)+chr(95)+chr(116)+chr(111)+chr(100)+chr(97)+chr(121))}")


async def check_modes() -> None:
    async with Client(server=target(MERCHANT, "MOCK_MODE", "inject_in_messages"), mode="auto") as client:
        _, _, state = await call(client, "create_checkout", {"sku": "taco", "quantity": 1})
        messages = state.get("messages", [])
        check("mode inject_in_messages: 购物车 messages 里带注入串",
              len(messages) == 1 and "SYSTEM NOTE" in messages[0], str(messages)[:70])

    async with Client(server=target(MERCHANT, "MOCK_MODE", "inject_in_catalog"), mode="auto") as client:
        _, _, obj = await call(client, "list_catalog", {"query": "taco"})
        items = obj.get("items", [])
        check("mode inject_in_catalog: 目录描述里带注入串",
              bool(items) and "SYSTEM NOTE" in items[0].get("description", ""),
              items[0].get("description", "")[:70] if items else "no items")

    async with Client(server=target(MERCHANT, "MOCK_MODE", "error5xx"), mode="auto") as client:
        err, text, _ = await call(client, "list_catalog", {})
        check("mode error5xx: 崩溃只给通用消息（原文只进日志）",
              err and text.strip() == "Error executing tool list_catalog", text[:80])

    async with Client(server=target(PSP, "PSP_MODE", "decline"), mode="auto") as client:
        _, _, intent = await call(client, "create_payment_intent",
                                  {"amount": 10.0, "merchant_id": "m1", "checkout_ref": "ck_d",
                                   "idempotency_key": "d1"})
        check("mode decline: 业务失败是「调用成功 + 状态失败」",
              intent.get("status") == "requires_payment_method" and not intent.get("payment_token"),
              str((intent.get("last_payment_error") or {}).get("code")))

    async with Client(server=target(PSP, "PSP_MODE", "duplicate_capture"), mode="auto") as client:
        _, _, intent = await call(client, "create_payment_intent",
                                  {"amount": 10.0, "merchant_id": "m1", "checkout_ref": "ck_dup",
                                   "idempotency_key": "p1"})
        intent_id = intent.get("payment_intent_id", "")
        await call(client, "capture_payment", {"payment_intent_id": intent_id, "idempotency_key": "p2"})
        _, _, again = await call(client, "capture_payment", {"payment_intent_id": intent_id, "idempotency_key": "p3"})
        check("mode duplicate_capture: 危险开关真的会重复扣款（T5 的靶子）",
              again.get("double_charged") is True, f"captured={again.get(chr(99)+chr(97)+chr(112)+chr(116)+chr(117)+chr(114)+chr(101)+chr(100)+chr(95)+chr(97)+chr(109)+chr(111)+chr(117)+chr(110)+chr(116))}")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="all", choices=["all", "merchant", "psp", "modes"])
    args = ap.parse_args()

    if args.only in ("all", "merchant"):
        await check_merchant()
    if args.only in ("all", "psp"):
        await check_psp()
    if args.only in ("all", "modes"):
        await check_modes()

    failed = [name for name, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
    if failed:
        print("failed: " + ", ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except BaseException as exc:  # pragma: no cover
        for leaf in leaves(exc):
            print("[FATAL]", type(leaf).__name__, ":", leaf)
        raise SystemExit(1)
