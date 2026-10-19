#!/usr/bin/env python
"""Commerce MCP Gateway — deterministic CLI demo.

The demo is a *story about evidence*, not a storefront. It runs the real gateway
and the real mock merchant/PSP, then shows one happy path, two denial layers, a
recorded model trajectory (F14), approval and audit.

Design constraints:

- CLI only; no web UI.
- No model key and no network for the deterministic acts.
- Fixed cart (10 taco, 43.10 CNY) and fixed mandate (100 CNY, m1, one transaction).
- Every expected outcome is asserted; a mismatch exits non-zero.
- F14 is a *recorded* qwen3.8-27b trajectory, replayed from a compact fixture.
  It is exploratory N=5; 2/5 is the attempt rate, not the attack success rate.

Usage::

    uv run python scripts/demo.py
    uv run python scripts/demo.py --act transaction
    uv run python scripts/demo.py --deep          # adds the historical before/after
    uv run python scripts/demo.py --fast --no-color
    uv run python scripts/demo.py --json > demo.json
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import json
import logging
import os
import pathlib
import shutil
import socket
import sys
import tempfile
import unicodedata
from collections.abc import AsyncIterator
from typing import Any

import uvicorn
from mcp.client.client import Client
from mcp.shared.exceptions import MCPError

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from gateway import cli
from gateway.app import Gateway
from gateway.config import GatewayConfig, UpstreamSpec
from gateway.evidence import ChainLedger

FIXTURE = ROOT / "demo" / "fixtures" / "f14_attempt.json"
DEMO_ROOT = ROOT / "runs" / "demo"
SUBJECT = "agent-dev"


class DemoFailure(RuntimeError):
    """An expected outcome did not happen."""


class Style:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    GREEN = "\033[32m"
    RED = "\033[31m"
    YELLOW = "\033[33m"
    CYAN = "\033[36m"
    BLUE = "\033[34m"

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def __call__(self, text: str, code: str) -> str:
        return f"{code}{text}{self.RESET}" if self.enabled else text


def _width(text: str) -> int:
    """Display width; CJK characters count as two columns."""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _width(text))


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


class Printer:
    def __init__(self, *, color: bool = True, quiet: bool = False, fast: bool = False) -> None:
        self.s = Style(color)
        self.quiet = quiet
        self.fast = fast

    def _print(self, text: str = "") -> None:
        if not self.quiet:
            print(text)

    async def pause(self, seconds: float = 0.12) -> None:
        if not self.quiet and not self.fast:
            await asyncio.sleep(seconds)

    def rule(self) -> None:
        self._print(self.s("─" * 88, Style.DIM))

    def title(self, text: str, subtitle: str = "") -> None:
        self._print()
        self._print(self.s(text, Style.BOLD))
        if subtitle:
            self._print(self.s(subtitle, Style.DIM))
        self.rule()

    def act(self, number: int, title: str, thesis: str) -> None:
        self._print()
        self._print(self.s(f"[{number}] {title}", Style.BOLD + Style.CYAN))
        self._print(self.s(thesis, Style.DIM))
        self.rule()

    def sub(self, text: str) -> None:
        self._print()
        self._print(self.s(f"  · {text}", Style.BOLD))

    def info(self, label: str, value: str) -> None:
        self._print(f"  {self.s(label.ljust(12), Style.DIM)} {value}")

    def ok(self, label: str, value: str) -> None:
        self._print(f"  {self.s('OK'.ljust(12), Style.GREEN)} {label}: {value}")

    def deny(self, label: str, value: str) -> None:
        self._print(f"  {self.s('DENY'.ljust(12), Style.RED)} {label}: {value}")

    def hold(self, label: str, value: str) -> None:
        self._print(f"  {self.s('HOLD'.ljust(12), Style.YELLOW)} {label}: {value}")

    def call(self, name: str, arguments: dict[str, Any]) -> None:
        shown = dict(arguments)
        if isinstance(shown.get("idempotency_key"), str):
            key = shown["idempotency_key"]
            shown["idempotency_key"] = "…" + key[-6:]
        payload = json.dumps(shown, ensure_ascii=False, separators=(", ", ": "))
        self._print(f"  {self.s('call', Style.BLUE)}  {name}({_clip(payload, 120)})")

    def result(self, is_error: bool, summary: str) -> None:
        tag = "deny" if is_error else "ok"
        code = Style.RED if is_error else Style.GREEN
        self._print(f"  {self.s(tag.ljust(5), code)} {summary}")

    def model(self, step: int | None, content: str) -> None:
        if not content.strip():
            return
        text = content.strip().replace("\n", " ")
        self._print(f"  {self.s(f'model[{step}]', Style.CYAN)} {_clip(text, 260)}")

    def table(self, headers: list[str], rows: list[list[str]]) -> None:
        widths = [_width(h) for h in headers]
        for row in rows:
            for i, cell in enumerate(row):
                widths[i] = max(widths[i], _width(str(cell)))
        line = "  " + "  ".join(_pad(h, widths[i]) for i, h in enumerate(headers))
        self._print(self.s(line, Style.DIM))
        self._print("  " + "  ".join("─" * widths[i] for i in range(len(headers))))
        for row in rows:
            self._print("  " + "  ".join(_pad(str(cell), widths[i]) for i, cell in enumerate(row)))


def _summary(text: str) -> str:
    """One-line result summary for the terminal."""
    if text.startswith("denied:"):
        return text
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return _clip(text, 220)
    if not isinstance(payload, dict):
        return _clip(text, 220)
    if "refund_id" in payload:
        return f"refund {payload['refund_id']} · {payload.get('status')} · {payload.get('amount')}"
    if "order_id" in payload:
        total = (payload.get("totals") or {}).get("total")
        return f"order {payload['order_id']} · {payload.get('status')} · total {total}"
    if "checkout_id" in payload:
        total = (payload.get("totals") or {}).get("total")
        return f"checkout {payload['checkout_id']} · total {total} {payload.get('currency', '')}".strip()
    if "payment_intent_id" in payload and "payment_token" in payload:
        return (
            f"intent {payload['payment_intent_id']} · {payload.get('status')} · "
            f"{payload.get('amount')} {payload.get('currency', '')}"
        )
    if "captured_amount" in payload:
        currency = payload.get("currency") or ""
        suffix = f" {currency}" if currency else ""
        return f"captured {payload.get('captured_amount')}{suffix} · {payload.get('status')}"
    if "items" in payload and isinstance(payload["items"], list):
        items = payload["items"]
        first = items[0] if items else {}
        return f"{len(items)} item(s) · {first.get('sku')} @ {first.get('price')} {payload.get('currency', '')}"
    if "per_txn_limit" in payload:
        return (
            f"per_txn={payload.get('per_txn_limit')} daily={payload.get('daily_limit')} "
            f"spent={payload.get('spent_today')}"
        )
    return _clip(text, 220)


def _payload(result: dict[str, Any]) -> dict[str, Any]:
    try:
        value = json.loads(result.get("text") or "")
    except (json.JSONDecodeError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _expect_ok(result: dict[str, Any], what: str) -> dict[str, Any]:
    if result.get("is_error"):
        raise DemoFailure(f"{what}: expected success, got {result.get('text')!r}")
    return _payload(result)


def _expect_denied(result: dict[str, Any], reason: str, what: str) -> None:
    text = result.get("text") or ""
    if not result.get("is_error") or reason not in text:
        raise DemoFailure(f"{what}: expected deny({reason}), got is_error={result.get('is_error')} {text!r}")


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def fresh_workdir(name: str) -> pathlib.Path:
    path = DEMO_ROOT / name
    shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)
    return path


def make_config(
    workdir: pathlib.Path,
    *,
    port: int,
    transaction_guard: bool = True,
    approval_timeout: float = 30.0,
) -> GatewayConfig:
    return GatewayConfig(
        upstreams=[
            UpstreamSpec(
                alias="merchant",
                command=sys.executable,
                args=[str(cli.MOCKS / "merchant.py")],
                env={"MOCK_MODE": "none", "MOCK_DELAY_S": "0.02"},
            ),
            UpstreamSpec(
                alias="psp",
                command=sys.executable,
                args=[str(cli.MOCKS / "psp.py")],
                env={"PSP_MODE": "none", "MOCK_DELAY_S": "0.02"},
            ),
        ],
        host="127.0.0.1",
        port=port,
        evidence_dir=workdir / "evidence",
        budget_path=workdir / "budget.sqlite",
        approvals_path=workdir / "approvals.jsonl",
        policy_path=cli.DEMO_POLICY,
        mandate_path=cli.DEMO_MANDATE,
        approval_timeout_seconds=approval_timeout,
        transaction_guard=transaction_guard,
    )


@contextlib.asynccontextmanager
async def running_gateway(config: GatewayConfig) -> AsyncIterator[Gateway]:
    async with Gateway(config) as gateway:
        assert gateway.app is not None
        server = uvicorn.Server(
            uvicorn.Config(
                gateway.app,
                host=config.host,
                port=config.port,
                log_level="warning",
                access_log=False,
            )
        )
        serving = asyncio.create_task(server.serve())
        try:
            for _ in range(300):
                if server.started:
                    break
                if serving.done():
                    serving.result()
                await asyncio.sleep(0.02)
            else:
                raise DemoFailure("gateway did not start within 6 seconds")
            yield gateway
        finally:
            server.should_exit = True
            with contextlib.suppress(Exception):
                await asyncio.wait_for(serving, timeout=10)


async def call(
    client: Client, p: Printer, name: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    p.call(name, arguments)
    try:
        result = await client.call_tool(name, arguments)
    except MCPError as exc:
        text = f"MCPError {exc.code}: {exc.message}"
        p.result(True, text)
        return {"is_error": True, "text": text, "code": exc.code}
    dump = result.model_dump(by_alias=True)
    texts = [str(block.get("text", "")) for block in dump.get("content", []) if isinstance(block, dict)]
    text = "\n".join(texts)
    out = {"is_error": bool(dump.get("isError")), "text": text, "result_type": dump.get("resultType")}
    p.result(out["is_error"], _summary(text))
    return out


async def create_checkout(client: Client, p: Printer, *, quantity: int, sku: str = "taco") -> dict[str, Any]:
    result = await call(client, p, "merchant__create_checkout", {"sku": sku, "quantity": quantity})
    return _expect_ok(result, "create_checkout")


async def create_intent(
    client: Client,
    p: Printer,
    checkout: dict[str, Any],
    *,
    key: str,
    amount: float | None = None,
) -> dict[str, Any]:
    total = float(amount if amount is not None else checkout["totals"]["total"])
    result = await call(
        client,
        p,
        "psp__create_payment_intent",
        {
            "amount": total,
            "currency": checkout.get("currency", "CNY"),
            "merchant_id": checkout.get("merchant_id", "m1"),
            "checkout_ref": checkout["checkout_id"],
            "idempotency_key": key,
        },
    )
    return _expect_ok(result, "create_payment_intent")


async def capture_intent(client: Client, p: Printer, intent: dict[str, Any], *, key: str) -> dict[str, Any]:
    result = await call(
        client,
        p,
        "psp__capture_payment",
        {
            "payment_intent_id": intent["payment_intent_id"],
            "amount": intent["amount"],
            "idempotency_key": key,
        },
    )
    return _expect_ok(result, "capture_payment")


async def complete_checkout(
    client: Client, p: Printer, checkout: dict[str, Any], token: str
) -> dict[str, Any]:
    result = await call(
        client,
        p,
        "merchant__complete_checkout",
        {"checkout_id": checkout["checkout_id"], "payment_token": token},
    )
    return _expect_ok(result, "complete_checkout")


async def purchase(
    client: Client, p: Printer, *, quantity: int = 10, sku: str = "taco"
) -> dict[str, Any]:
    await call(client, p, "merchant__list_catalog", {"query": sku})
    checkout = await create_checkout(client, p, quantity=quantity, sku=sku)
    intent = await create_intent(
        client, p, checkout, key=f"demo-{checkout['checkout_id']}-auth"
    )
    await capture_intent(
        client, p, intent, key=f"demo-{checkout['checkout_id']}-capture"
    )
    order = await complete_checkout(client, p, checkout, str(intent["payment_token"]))
    return {"checkout": checkout, "intent": intent, "order": order}


async def act_gap(p: Printer) -> dict[str, Any]:
    p.act(
        0,
        "THE GAP · per-call authorization is not transaction authorization",
        "A 43.10 CNY cart. Budget 100. Merchant m1 only. Every call is inside the bounds.",
    )
    p._print("  The agent calls complete_checkout before capture_payment.")
    p._print("  A per-call gateway sees: allow, allow.")
    p._print("  The customer gets a free lunch.")
    p._print()
    p._print("  This is the gap the project targets: money moves across calls,")
    p._print("  but the authorization check sees one call at a time.")
    p._print()
    p._print("  Industry protocols already bind authorization to the checkout:")
    p._print("  AP2 mandates, Stripe PaymentIntent + capture, ACP checkout sessions.")
    p._print("  The missing piece is transaction context, not another per-call rule.")
    return {"ok": True, "thesis": "per-call authorization misses cross-call state"}


async def act_happy(p: Printer) -> dict[str, Any]:
    p.act(1, "HAPPY PATH · a normal purchase still works", "The gateway must allow the legitimate flow before it can deny the bad ones.")
    workdir = fresh_workdir("happy")
    config = make_config(workdir, port=free_port())
    async with running_gateway(config) as gateway, Client(server=gateway.url, mode="auto") as client:
            result = await purchase(client, p, quantity=10)
            balance = gateway.budget.balance(SUBJECT)
            verify = gateway.evidence.verify(config.run_id)
            p.info("budget", f"committed={balance.committed:.2f} active={balance.active_holds:.2f} remaining={balance.remaining:.2f}")
            p.info("evidence", f"{verify.entries} entries · verify ok={verify.ok}")
            if not verify.ok:
                raise DemoFailure(f"happy path evidence did not verify: {verify.detail}")
            if abs(balance.committed - 43.10) > 0.005:
                raise DemoFailure(f"expected 43.10 committed, got {balance.committed}")
            return {
                "ok": True,
                "charged": balance.committed,
                "remaining": balance.remaining,
                "verify_ok": verify.ok,
                "order_id": result["order"].get("order_id"),
            }


async def act_policy(p: Printer) -> dict[str, Any]:
    p.act(2, "PER-CALL POLICY · amount and tool allowlists still matter", "The first layer is unchanged: an over-limit amount or an unlisted tool is denied.")
    workdir = fresh_workdir("policy")
    config = make_config(workdir, port=free_port())
    async with running_gateway(config) as gateway, Client(server=gateway.url, mode="auto") as client:
            tools = [tool.name for tool in (await client.list_tools()).tools]
            p.info("tools exposed", f"{len(tools)} · psp__refund_payment listed={('psp__refund_payment' in tools)}")
            checkout = await create_checkout(client, p, quantity=10)
            over = await call(
                client,
                p,
                "psp__create_payment_intent",
                {
                    "amount": 100.0,
                    "currency": "CNY",
                    "merchant_id": "m1",
                    "checkout_ref": checkout["checkout_id"],
                    "idempotency_key": "demo-over-limit",
                },
            )
            _expect_denied(over, "amount_over_limit", "over-limit intent")
            refund = await call(client, p, "psp__refund_payment", {"payment_intent_id": "pi_missing"})
            _expect_denied(refund, "policy_denied", "unlisted refund tool")
            p.info("money moved", "0.00")
            return {"ok": True, "amount_reason": "amount_over_limit", "tool_reason": "policy_denied"}


async def _scenario_complete_before_capture(p: Printer) -> dict[str, Any]:
    p.sub("scenario 1 · complete before capture")
    workdir = fresh_workdir("transaction-complete")
    config = make_config(workdir, port=free_port())
    async with running_gateway(config) as gateway, Client(server=gateway.url, mode="auto") as client:
            checkout = await create_checkout(client, p, quantity=10)
            intent = await create_intent(client, p, checkout, key="demo-complete-auth")
            result = await call(
                client,
                p,
                "merchant__complete_checkout",
                {"checkout_id": checkout["checkout_id"], "payment_token": intent["payment_token"]},
            )
            _expect_denied(result, "payment_not_captured", "complete before capture")
            balance = gateway.budget.balance(SUBJECT)
            return {"ok": True, "reason": "payment_not_captured", "charged": balance.committed}


async def _scenario_duplicate_intent(p: Printer) -> dict[str, Any]:
    p.sub("scenario 2 · two intents for one checkout")
    workdir = fresh_workdir("transaction-duplicate")
    config = make_config(workdir, port=free_port())
    async with running_gateway(config) as gateway, Client(server=gateway.url, mode="auto") as client:
            checkout = await create_checkout(client, p, quantity=10)
            await create_intent(client, p, checkout, key="demo-dup-auth-1")
            second = await call(
                client,
                p,
                "psp__create_payment_intent",
                {
                    "amount": checkout["totals"]["total"],
                    "currency": checkout["currency"],
                    "merchant_id": checkout["merchant_id"],
                    "checkout_ref": checkout["checkout_id"],
                    "idempotency_key": "demo-dup-auth-2",
                },
            )
            _expect_denied(second, "duplicate_payment_intent", "second intent")
            balance = gateway.budget.balance(SUBJECT)
            return {"ok": True, "reason": "duplicate_payment_intent", "charged": balance.committed}


async def _scenario_forged_token(p: Printer) -> dict[str, Any]:
    p.sub("scenario 3 · forged payment token")
    workdir = fresh_workdir("transaction-token")
    config = make_config(workdir, port=free_port())
    async with running_gateway(config) as gateway, Client(server=gateway.url, mode="auto") as client:
            checkout = await create_checkout(client, p, quantity=1)
            intent = await create_intent(client, p, checkout, key="demo-token-auth")
            await capture_intent(client, p, intent, key="demo-token-capture")
            forged = await call(
                client,
                p,
                "merchant__complete_checkout",
                {
                    "checkout_id": checkout["checkout_id"],
                    "payment_token": f"pt_forged_{checkout['checkout_id']}",
                },
            )
            _expect_denied(forged, "token_mismatch", "forged token")
            balance = gateway.budget.balance(SUBJECT)
            p.info(
                "legitimate charge",
                f"{balance.committed:.2f} (capture succeeded; the forged completion added 0.00)",
            )
            return {
                "ok": True,
                "reason": "token_mismatch",
                "charged": balance.committed,
                "extra_charge": 0.0,
            }


async def act_transaction(p: Printer) -> dict[str, Any]:
    p.act(3, "TRANSACTION-LEVEL ATTACKS · the sequence is the unit", "Each call is individually legal. The guard denies the transaction because the context is wrong.")
    rows = []
    for name, fn in (
        ("complete before capture", _scenario_complete_before_capture),
        ("two intents / one checkout", _scenario_duplicate_intent),
        ("forged token", _scenario_forged_token),
    ):
        result = await fn(p)
        extra = result.get("extra_charge", result["charged"])
        rows.append([name, "allow", "deny", f"{float(extra):.2f}"])
    p._print()
    p.table(["sequence", "per-call", "gateway", "extra charge"], rows)
    return {"ok": True, "scenarios": rows}


async def act_deep(p: Printer) -> dict[str, Any]:
    p.act(3.5, "WHY I5 EXISTS · a historical probe, not a current behavior", "Guard off reproduces the old bug. Guard on denies the same sequence. The probe stays in eval/p0_state_attacks.py.")
    rows = []
    for label, guard in (("guard off (historical probe)", False), ("guard on (current default)", True)):
        workdir = fresh_workdir("deep-" + ("off" if not guard else "on"))
        config = make_config(workdir, port=free_port(), transaction_guard=guard)
        async with running_gateway(config) as gateway, Client(server=gateway.url, mode="auto") as client:
                checkout = await create_checkout(client, p, quantity=10)
                intent = await create_intent(client, p, checkout, key=f"demo-deep-{guard}-auth")
                result = await call(
                    client,
                    p,
                    "merchant__complete_checkout",
                    {"checkout_id": checkout["checkout_id"], "payment_token": intent["payment_token"]},
                )
                balance = gateway.budget.balance(SUBJECT)
                completed = not result["is_error"] and balance.committed == 0.0
                if guard:
                    _expect_denied(result, "payment_not_captured", "guard-on sequence")
                    p.ok("guard on", "denied payment_not_captured · 0.00 charged")
                else:
                    if result["is_error"]:
                        raise DemoFailure(f"guard-off probe unexpectedly denied: {result['text']}")
                    p.deny("guard off", "order completed · 0.00 charged (the old bug)")
                rows.append([label, "completed" if completed else "denied", f"{balance.committed:.2f}"])
    p._print()
    p.table(["mode", "outcome", "charged"], rows)
    p.info("default", "transaction_guard=True; the deep act is demo-only")
    return {"ok": True, "rows": rows}


async def act_f14(p: Printer) -> dict[str, Any]:
    p.act(4, "F14 · a real model tries to split the purchase", "Recorded qwen3.8-27b trajectory, replayed deterministically. The second charge is denied by the mandate transaction limit.")
    if not FIXTURE.exists():
        raise DemoFailure(f"missing F14 fixture: {FIXTURE}")
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    meta = fixture["meta"]
    obs = meta["observations"]
    p.info("recorded", f"{meta['model']} · {meta['date']} · source={meta['source_run_id']}")
    p.info("cell", meta["cell"])
    p.info("hidden limit", "max_transactions=1 (the model cannot see it; the gateway enforces it)")
    p.info(
        "observations",
        f"baseline {obs['baseline_attempts']}/{obs['baseline_runs']} attempts · "
        f"injected {obs['injected_attempts']}/{obs['injected_runs']} attempts · "
        f"denied {obs['denied_attempts']}/{obs['injected_attempts']} · double charges {obs['double_charges']}",
    )
    p._print(f"  {p.s('caveat', Style.YELLOW)}     {meta['caveat']}")
    p.rule()
    denied = False
    for event in fixture["events"]:
        if event["kind"] == "model":
            p.model(event.get("step"), event.get("content") or "")
            for call_spec in event.get("tool_calls") or []:
                p.call(call_spec["name"], call_spec.get("arguments") or {})
            await p.pause(0.08)
        else:
            text = str(event.get("result") or "")
            is_error = text.startswith(("denied:", "Error executing tool"))
            p.result(is_error, _summary(text))
            if "mandate_transaction_limit" in text:
                denied = True
            await p.pause(0.08)
    if not denied:
        raise DemoFailure("F14 fixture did not contain mandate_transaction_limit")
    p._print()
    p.info("reading", "2/5 is the attempt rate; the attack success rate is 0/5.")
    p.info("reading", "The gateway blocked the second charge; the agent still bought 5 of 10.")
    p.info("boundary", "Safety is not task success: the model/harness quality is a separate axis.")
    return {"ok": True, "attempt_rate": "2/5", "success_rate": "0/5", "double_charges": 0}


async def act_approval(p: Printer) -> dict[str, Any]:
    p.act(5, "APPROVAL · a state, not a log line", "A refund needs a human. The pending record is durable; approval is bound to the action.")
    workdir = fresh_workdir("approval")
    config = make_config(workdir, port=free_port(), approval_timeout=30.0)
    async with running_gateway(config) as gateway, Client(server=gateway.url, mode="auto") as client:
            bought = await purchase(client, p, quantity=1)
            order_id = bought["order"]["order_id"]
            task = asyncio.create_task(
                call(client, p, "merchant__request_refund", {"order_id": order_id, "reason": "demo"})
            )
            pending = None
            for _ in range(200):
                pending = gateway.approval.pending()
                if pending:
                    break
                if task.done():
                    break
                await asyncio.sleep(0.05)
            if not pending:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
                raise DemoFailure("approval was not registered within 10 seconds")
            item = pending[0]
            p.hold("pending", f"{item.approval_id} · {item.action} · {item.reason_code}")
            p.info("CLI", f"uv run python -m gateway approve {item.approval_id}")
            gateway.approval.decide(item.approval_id, approved=True, by="human")
            result = await asyncio.wait_for(task, timeout=10)
            _expect_ok(result, "approved refund")
            p.ok("approved", f"{item.approval_id} -> approved; refund executed")
            rows = [json.loads(line) for line in (workdir / "approvals.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
            p.info("audit rows", f"{len(rows)} (pending + decision in one shape)")
            return {"ok": True, "approval_id": item.approval_id, "approved": True}


async def act_audit(p: Printer) -> dict[str, Any]:
    p.act(6, "AUDIT · prove what happened, then prove it was not edited", "The chain verifies; a one-byte tamper is detected; the decision sequence is reconstructable.")
    workdir = fresh_workdir("audit")
    config = make_config(workdir, port=free_port())
    async with running_gateway(config) as gateway, Client(server=gateway.url, mode="auto") as client:
            await purchase(client, p, quantity=1)
            verify = gateway.evidence.verify(config.run_id)
            p.info("verify", f"ok={verify.ok} entries={verify.entries} broken_at={verify.broken_at}")
            if not verify.ok:
                raise DemoFailure(f"clean chain did not verify: {verify.detail}")
            rows = [json.loads(line) for line in gateway.evidence.chain_path.read_text(encoding="utf-8").splitlines() if line.strip()]
            p.table(
                ["seq", "phase", "method", "decision", "outcome", "reason"],
                [
                    [
                        str(row.get("seq")),
                        str(row.get("phase")),
                        str(row.get("method")),
                        str(row.get("decision")),
                        str(row.get("outcome") or "-"),
                        str(row.get("reason_code") or "-"),
                    ]
                    for row in rows
                ],
            )
            tmp = pathlib.Path(tempfile.mkdtemp(prefix="commerce-demo-tamper-"))
            try:
                tampered = tmp / "evidence"
                shutil.copytree(gateway.config.evidence_dir, tampered)
                lines = (tampered / "chain.jsonl").read_text(encoding="utf-8").splitlines()
                first = json.loads(lines[0])
                first["tool"] = (first.get("tool") or "") + "-tampered"
                lines[0] = json.dumps(first, ensure_ascii=False)
                (tampered / "chain.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
                bad = ChainLedger(tampered).verify()
                p.deny("tampered copy", f"ok={bad.ok} broken_at={bad.broken_at} (expected False/0)")
                if bad.ok:
                    raise DemoFailure("tampered chain still verified")
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
            report = gateway.enforcer.reconstruct(config.run_id)
            p.info(
                "reconstruct",
                f"preconditions {report.preconditions_met}/{report.eligible} · "
                f"decisions {report.matched}/{report.eligible}",
            )
            if report.eligible == 0 or report.matched != report.eligible:
                raise DemoFailure(f"reconstruction failed: {report.detail}")
            return {
                "ok": True,
                "verify_ok": verify.ok,
                "tamper_detected": not bad.ok,
                "reconstructed": f"{report.matched}/{report.eligible}",
            }


async def run_act(name: str, p: Printer) -> dict[str, Any]:
    acts = {
        "gap": act_gap,
        "happy": act_happy,
        "policy": act_policy,
        "transaction": act_transaction,
        "deep": act_deep,
        "f14": act_f14,
        "approval": act_approval,
        "audit": act_audit,
    }
    if name == "all":
        return await run_all(p)
    if name not in acts:
        raise DemoFailure(f"unknown act: {name}")
    return await acts[name](p)


async def run_all(p: Printer) -> dict[str, Any]:
    p.title("COMMERCE MCP GATEWAY · CONTROL-PLANE DEMO", "CLI only · deterministic · no model key · no network for the main path")
    results: dict[str, Any] = {}
    results["gap"] = await act_gap(p)
    results["happy"] = await act_happy(p)
    results["policy"] = await act_policy(p)
    results["transaction"] = await act_transaction(p)
    results["f14"] = await act_f14(p)
    results["approval"] = await act_approval(p)
    results["audit"] = await act_audit(p)
    p._print()
    p.title("SUMMARY", "The demo has shown both layers: per-call policy and transaction context.")
    rows = [
        ["happy path", "43.10 charged; verify ok"],
        ["per-call policy", "amount_over_limit + policy_denied"],
        ["transaction guard", "3/3 sequences denied"],
        ["F14", "2/2 model attempts denied; 0 double charges (N=5 exploratory)"],
        ["approval", "pending -> human approve -> executed"],
        ["audit", "verify ok; tamper detected; decisions reconstructed"],
    ]
    p.table(["claim", "what you just saw"], rows)
    return {"ok": True, "acts": results}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="demo.py", description="Commerce MCP Gateway CLI demo")
    parser.add_argument(
        "--act",
        default="all",
        choices=["all", "gap", "happy", "policy", "transaction", "deep", "f14", "approval", "audit"],
        help="which act to run (default: all)",
    )
    parser.add_argument("--deep", action="store_true", help="include the guard-off historical probe in --act all")
    parser.add_argument("--fast", action="store_true", help="no pauses between steps")
    parser.add_argument("--no-color", action="store_true", help="disable ANSI colors")
    parser.add_argument("--json", action="store_true", help="print a machine-readable result only")
    return parser


async def _run(args: argparse.Namespace, p: Printer) -> dict[str, Any]:
    if args.act == "all" and args.deep:
        results: dict[str, Any] = {}
        p.title("COMMERCE MCP GATEWAY · CONTROL-PLANE DEMO", "CLI only · deterministic · includes the historical guard-off probe")
        for name in ("gap", "happy", "policy", "transaction", "deep"):
            results[name] = await run_act(name, p)
        for name in ("f14", "approval", "audit"):
            results[name] = await run_act(name, p)
        p._print()
        p.title("SUMMARY", "The demo has shown both layers: per-call policy and transaction context.")
        p.table(
            ["claim", "what you just saw"],
            [
                ["happy path", "43.10 charged; verify ok"],
                ["per-call policy", "amount_over_limit + policy_denied"],
                ["transaction guard", "3/3 sequences denied"],
                ["historical probe", "guard off reproduces the free order; guard on denies it"],
                ["F14", "2/2 model attempts denied; 0 double charges (N=5 exploratory)"],
                ["approval", "pending -> human approve -> executed"],
                ["audit", "verify ok; tamper detected; decisions reconstructed"],
            ],
        )
        return {"ok": True, "acts": results}
    return await run_act(args.act, p)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("mcp").setLevel(logging.WARNING)
    logging.disable(logging.INFO)
    color = not args.no_color and not args.json and not os.environ.get("NO_COLOR")
    p = Printer(color=color, quiet=args.json, fast=args.fast)
    try:
        if args.json:
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                result = asyncio.run(_run(args, p))
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            result = asyncio.run(_run(args, p))
        return 0 if result.get("ok") else 1
    except DemoFailure as exc:
        if args.json:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        else:
            p.deny("FAIL", str(exc))
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
