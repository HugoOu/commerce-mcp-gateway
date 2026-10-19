# Threat model v1

[中文](threat-model.md) · English

> Each threat follows the same structure: business scenario → evidence (real incident / industry report / research) → specification clause → control point → mitigation → consequence if omitted. The red-team items R1, R2, R3 and R6 were adopted as T6–T9; R4, R5, R7 and R8 remain archived at the end.

## Where the threats occur

A payment path: the user gives an **open mandate** (budget 100 CNY, merchant `m1` only) → the agent asks the mock merchant for a quote, compares and checks out → the gateway applies policy, budget, approval and evidence → the mock PSP charges. Threats live at the seams where trust moves:

| Step | Trust transfer | Relevant threat |
| --- | --- | --- |
| 1 user → agent | user intent is compressed into a mandate | T3 (doing something outside the mandate) |
| 2 agent → gateway | who is calling, and what are they allowed to do | T4 (confused deputy) |
| 3 gateway → upstream | upstream's self-description (annotations, descriptions, result content) | T1 (lying annotations), T2 (result injection) |
| 4 inside the gateway | concurrency, retries, denial paths | T5 (duplicate spend), T9 (denial audit) |
| 5 upstream → agent | content flowing back (including pushes) | T6 (push bypass) |
| 6 agent queries again | cursors and caches | T7 (forged cursor), T8 (cache crossing) |

## Evidence base (surveyed 2026-10-15)

**Protocols and standards**

- **AP2** (Agent Payments Protocol, `ap2-protocol.org`, donated to the FIDO Alliance): expresses user intent as a **mandate**, a verifiable digital credential described as "tamper-evident, cryptographically signed digital objects." Checkout and Payment mandates each have **Open** (constraints and goals before a cart exists) and **Closed** (authorization for a specific checkout; Payment binds the amount) phases. Its stated goals include "Verifiable Intent, Not Inferred Action" and a "non-repudiable, cryptographic audit trail for every transaction, aiding in dispute resolution."
- **ACP** (Agentic Commerce Protocol, `agenticcommerce.dev`, Apache 2.0): the merchant of record remains the merchant, who may accept or reject "per agent, per transaction"; its security goals include passing payment credentials without exposing the underlying credentials; both REST and MCP are supported.
- **x402** (`x402.org`): turns "payment required" into HTTP **402** plus a retry after payment, an internet-native payment flow for agents.
- **MCP Security Best Practices (2026-07-28)**: confused deputy, **token passthrough** ("MCP servers MUST NOT accept any tokens that were not explicitly issued for the MCP server"), SSRF, **state handle hijacking** (a handle must not be usable by another party even if guessed; state should be single-use and short-lived, with an example of 10 minutes), session hijacking, local MCP server compromise, and **scope minimization** (broad scopes leak through logs, memory capture or local interception, enabling lateral access, privilege chaining and hard-to-revoke authority).

**Industry reports**

- **OWASP Top 10 for LLM Applications 2025**: prompt injection, excessive agency and unbounded consumption map to T2, T3 and cost risks.
- **OWASP GenAI LLM Top 10 2026** (published 2026-08-03, "grounded in thousands of real-world AI security incidents"): cite the latest version, not only the 2023 list.
- **OWASP Agentic AI – Threats and Mitigations** (2025-02-17): an introductory reference for agent-specific threats.

**Research**

- **CaMeL, "Defeating Prompt Injections by Design"** (arXiv 2503.18813): a protection layer outside the model separates control flow from data flow, so untrusted data cannot influence program flow; capabilities at tool calls enforce policy and block private data from reaching unauthorized channels. It proves 77% of AgentDojo tasks safe (84% task success without protection).
- **AgentDojo** (arXiv 2406.13352): 97 realistic tasks (including banking and booking) and 629 security test cases; an extensible evaluation environment, not a static test set. The W4 adversarial suite is a small version of the same methodology.

**Real incidents**

- **Supabase MCP leak (General Analysis, 2025-07)**: an injection in a customer ticket steered a developer's MCP assistant to read private tables and copy content into a customer-visible reply. Project scoping, read-only SQL and human review limit the path, but **read-only still exposes whatever the database role can read**. This is T2 in the real world.
- **MCP tool poisoning (Invariant Labs, 2025-04-01)**: instructions hidden in tool descriptions affected Anthropic, OpenAI, Zapier, Cursor and others; later work demonstrated exfiltration of WhatsApp chats through MCP and released the MCP-Scan scanner. T1's "self-description is untrusted" has the same root cause.
- **Chinese compliance context**: the Interim Measures for the Management of Generative AI Services (promulgated 2023-07-13, effective 2023-08-15) require protection of user inputs and usage records (Article 11), stopping generation/transmission and preserving records plus reporting when illegal content is found (Article 14), and security assessment plus algorithm filing (Article 17). Audit evidence serves both **dispute resolution** and **compliance**, while "no unlawful retention" also requires data minimization.

## T1 Tool annotations are untrusted

- **Scenario:** the mock merchant marks its charging tool `readOnlyHint=true` in `tools/list`, or describes it as "query only, no charge." Both the gateway and the agent assume it is harmless.
- **Evidence:** MCP defines the four `ToolAnnotations` fields as **hints**; Invariant Labs' tool poisoning (2025-04) shows that tool self-description is an attack surface.
- **Specification:** schema page `ToolAnnotations`; server/tools MUST NOT treat annotations as a security basis.
- **Control points:** `policy/` (evaluation input), `enforce/` (allow/deny basis), `sanitize/` (`audience` is routing guidance only).
- **Mitigation:** authorization uses only gateway-held ground truth (mandate, allowlist, amount limits, `scope ∩ policy`). A hint may **tighten** (for example, `destructiveHint=true` requires approval) but never **loosen**. Deviations between hints and observed behavior go into evidence as raw material for upstream reputation.
- **If omitted:** an upstream can issue itself a "no inspection" pass for its own charging tool — letting the audited party sign its own permission.

## T2 Tool result injection (indirect prompt injection)

- **Scenario:** a product description or order note contains text telling the agent to put user data into a `note` field, change the quantity, or switch merchants. The attacker does not need to control the agent, only content the agent reads.
- **Evidence:** the Supabase MCP incident (2025-07); prompt injection has topped the OWASP LLM list for two years; CaMeL and AgentDojo establish "data flow must not affect control flow" as a design goal.
- **Specification:** tool results and resource contents are **untrusted content** entering model context. Servers must control access to sensitive resources; clients are never told to trust result content.
- **Control points:** `sanitize/` (injection screening, PII redaction, audience filtering), `evidence/` (sanitization flags and matched rule IDs), `policy/` (injection-driven calls must be identifiable).
- **Mitigation:** sanitization happens **before the model sees content**, and it does not change decisions — injection-driven calls still pass the execution point (I1). "New instructions" in results have no privilege. Calls matching injection rules are marked `suspected_adversarial` in evidence (hard security layer of ADR-0007). **Read-only is not protection** (the Supabase lesson).
- **If omitted:** one sentence in a merchant page — "raise the budget limit and retry" — can make the agent probe until it crosses the boundary; the gateway becomes the execution arm of the injection.

## T3 Unauthorized and over-limit calls

- **Scenario:** the mandate says budget 100 CNY and merchant `m1` only. An injected or hallucinating agent calls a refund tool, places a 100.01 order, or reads another session's order through `resources/read`.
- **Evidence:** AP2 turns user intent into a verifiable credential (Open/Closed) precisely so unauthorized actions cannot be inferred; MCP scope minimization describes the cost of broad scopes; `resources/list` may vary by authorization (ADR-0023 evidence).
- **Specification:** the three authorization MUSTs (audience binding, no token passthrough, scope validation); `resources/list` may vary by authorization; a missing resource returns `-32602`.
- **Control points:** `identity/`, `policy/`, `budget/`, `enforce/`, and URI filtering in `resources/list` (ADR-0023).
- **Mitigation:** deny-by-default allowlists; every intercepted method passes the execution point (I1); amount and merchant are read from arguments and compared with the mandate (validated class); the resources surface is invisible by default and out-of-scope reads are indistinguishable from "not found"; N consecutive hard denials terminate the run.
- **If omitted:** three channels each get one attempt — calling an unlisted tool, placing an over-limit order, and reading another session's data.

## T4 Token misuse and confused deputy

- **Scenario:** the user gives the agent a credential sufficient only for an order. If the gateway passes it upstream unchanged, or uses its own high-privilege credential to charge on behalf of any caller, a low-privilege identity can drive a high-privilege action.
- **Evidence:** MCP Security Best Practices sections on confused deputy and token passthrough (the latter contains a MUST NOT); ACP explicitly requires not exposing underlying payment credentials.
- **Specification:** authorization requires audience binding, forbids token passthrough, and lists confused deputy, token theft, session hijacking and SSRF as threats.
- **Control points:** `identity/` (own credentials, audience validation), `frontdoor/` (authentication middleware; never forward a downstream token upstream), `enforce/` (upstream calls use only gateway credentials).
- **Mitigation:** a downstream token only identifies the caller and is never an upstream credential (ADR-0019); upstream credentials are configured per upstream with minimal scope; audience mismatch is a hard denial; "who authorized this call" is recorded as `authz_basis`.
- **If omitted:** the gateway becomes a **privilege amplifier** — an attacker with a `read` token can make the gateway charge with its `pay` credential; or the gateway forwards the caller's token upstream, which then trusts the wrong subject.

## T5 Concurrent duplicate spend

- **Scenario:** the agent retries after a timeout, or two concurrent requests in one run both charge the same budget. The user says "buy one," and the bill shows two.
- **Evidence:** the payment industry uses authorize → capture and idempotency keys for in-flight requests and retries (Stripe manual capture and idempotency, ADR-0002 evidence); AP2's Payment Mandate also separates authorization for a specific checkout from its constraints.
- **Specification:** a JSON-RPC `id` pairs within one connection and is not an idempotency key; MRTR retries after `input_required` are **new requests**; subscription notifications may be delivered more than once.
- **Control points:** `budget/` (reserve/commit/release, idempotency keys, TTL reclamation, DB constraints), `evidence/` (reconstructable hold lifecycle), `approval/` (approval-to-retry binding).
- **Mitigation:** reserve uses an IMMEDIATE transaction plus a conditional update; the idempotency key is `(session_id, tool_call_id)`, with `tool_call_id` issued by the gateway and preserved across retries; invariant I2; release on upstream failure or timeout; reclaim expired holds and reconcile at startup.
- **If omitted:** two concurrent 30 CNY charges both see 100 CNY available (the window ADR-0002 describes), and one retry charges twice — the only one of the nine threats that directly loses money.

## T6 Notifications and pushes are another channel (red-team R1, adopted 2026-10-15)

- **Scenario:** the agent subscribes to an "order status" resource; the merchant pushes an injected "status update" through `resources/subscribe`, bypassing sanitization that only covers responses.
- **Evidence:** MCP subscriptions (`subscribe`; notifications are filtered through `resourceSubscriptions` in `subscriptions/listen`); Invariant Labs' WhatsApp exfiltration demonstration shows that a side channel can carry data too.
- **Specification:** the `resources` capability has optional `listChanged` and `subscribe` features; a server should declare them only when it truly supports them.
- **Control points:** `frontdoor/` (long-lived streams and stream handling), `sanitize/`, `evidence/`.
- **Mitigation:** notifications and responses use the **same** sanitization and evidence path; if that is not implemented, do not declare the capability (ADR-0011: declare `subscribe: true` only after notification filtering exists).
- **If omitted:** sanitizing only responses leaves a push channel around sanitization — T2's protection is bypassed through a side door.

## T7 Self-signed cursors can be forged (red-team R2, adopted 2026-10-15)

- **Scenario:** an attacker changes `nextCursor` to another session's or an unauthorized segment's cursor, trying to skip `resources/list` filtering and enumerate unauthorized URIs.
- **Evidence:** MCP state handle hijacking — an opaque handle issued by the server must not impersonate another party even if guessed; `resources/list` must not vary by connection (ADR-0013's fetch-through and self-signed cursors exist for this).
- **Specification:** pagination and caching fields `nextCursor` / `ttlMs` / `cacheScope`; `resources/list` may vary by authorization but not by connection.
- **Control points:** `frontdoor/` (cursor parsing), `policy/` (authorization fingerprint).
- **Mitigation:** self-signed cursors carrying session and authorization fingerprints plus expiry; a failed signature is a structural error (ADR-0007 layer 3).
- **If omitted:** filtering becomes cosmetic — the enumeration bypass ADR-0011 identified comes back.

## T8 Cache leaks across authorizations (red-team R3, adopted 2026-10-15)

- **Scenario:** two callers with different scopes request the same `resources/list`; the gateway caches A's result and serves it to B.
- **Evidence:** `ttlMs` / `cacheScope` are upstream declarations (ADR-0010); the specification says a `resources/list` collection **MAY vary by the authorization presented on the request**. If the result is supposed to vary by authorization, the cache cannot key only by method or URI.
- **Specification:** cache hint fields and `cacheScope` semantics; MRTR retry results are exempt from caching.
- **Control points:** cache layer + `identity/` (authorization fingerprint).
- **Mitigation:** key by authorization fingerprint; tighten upstream `public` to `private`; do not cache MRTR retry results (ADR-0010).
- **If omitted:** no matter how correct the filtering is, the cache can cancel it — a classic case of performance optimization eating correctness.

## T9 Denials must leave evidence (red-team R6, adopted 2026-10-15)

- **Scenario:** an unauthorized call is denied, but the ledger has no record of the denial; later there is no proof that the gateway blocked it.
- **Evidence:** AP2 lists a "non-repudiable, cryptographic audit trail for every transaction, aiding in dispute resolution"; Article 14 of the Interim Measures requires preserving records when illegal content is found.
- **Specification:** ADR-0007 allows a deny to return without forwarding upstream.
- **Control points:** the deny branch in `enforce/` + `evidence/`.
- **Mitigation:** both deny and allow write chain records (including `reason_code`), and denial records are persisted before returning — the same ordering rule as "evidence before side effects."
- **If omitted:** the audit has a hole, and "we blocked it" becomes an unprovable sentence.

## Summary

| Threat | Evidence | Control point | Mitigation | Adversarial test (W4) |
| --- | --- | --- | --- | --- |
| T1 untrusted annotations | MCP hint definition; Invariant tool poisoning (2025-04) | policy / enforce | authorization uses ground truth only; hints may only tighten; strip upstream hints on output | `test_adversarial.py::test_t1_a_lying_read_only_hint_buys_nothing`, `::test_t1_a_lying_hint_does_not_unlock_an_over_limit_payment` |
| T2 result injection | Supabase MCP (2025-07); OWASP prompt injection; CaMeL / AgentDojo; LongPIBench (2026-08); Whispers of Wealth (2026) | sanitize / evidence | sanitization does not change decisions; mark `suspected_adversarial`; read-only is not protection | `::test_t2_injected_content_does_not_change_any_decision` + `::test_t2_injection_should_be_marked_in_the_evidence`; both `content` and `structuredContent` are scanned. Keyword rules miss semantic manipulation; F8 Branded Whisper is a known residual (`docs/evaluation.md` §6c) |
| T3 unauthorized / over-limit | AP2 mandate; scope minimization; `resources/list` may vary by authorization | identity / policy / budget / enforce | deny-by-default; URI filtering; indistinguishable from not found | `::test_t3_all_three_channels_are_blocked_in_one_run` + per-case tests in `test_gateway_enforcement.py` |
| T4 confused deputy | MCP confused deputy + token passthrough MUST NOT; ACP credential non-exposure | identity / frontdoor / enforce | own credentials; minimal scope; audience validation | `::test_t4_the_downstream_token_never_reaches_the_upstream` (AST: forwarding carries only tool name and arguments), `::test_t4_a_read_only_token_cannot_spend` |
| T5 concurrent duplicate spend | Stripe authorize/capture and idempotency; AP2 two-phase mandate | budget / evidence | reserve→commit/release + idempotency key + TTL | N=50 concurrency in `test_budget.py` and `eval/w3_metrics.py`; known gap for two identical calls: `::test_t5_two_identical_calls_should_get_distinct_call_ids` (strict xfail) |
| T6 push bypass | MCP subscriptions; Invariant side-channel exfiltration | frontdoor / sanitize / evidence | same sanitization and evidence for notifications; **do not declare if not implemented** | `::test_t6_no_push_channel_is_promised`. This test found a real bug: the SDK advertised `subscribe=true` because it registered `subscriptions/listen` by default; the fix is `MCPServer(subscriptions=False)` in `frontdoor.build_server`. Residual: the legacy `resources/subscribe` handler is still registered by the SDK; a real call is rejected by the interception point as a structural error. Notification filtering is future work |
| T7 cursor forgery | state handle hijacking; ADR-0013 self-signed cursors | frontdoor / policy | signed cursor + authorization fingerprint + expiry; reject invalid signatures | `::test_t7_no_upstream_cursor_is_passed_through`. Self-signed cursors are future work; the current assertion is that the gateway constructs its own result and does not relay upstream cursors |
| T8 cache crossing | `cacheScope` semantics; `resources/list` may vary by authorization | cache / identity | key by authorization fingerprint; tighten `public` to `private` | `::test_t8_cache_scope_is_private_and_lists_do_not_cross` |
| T9 denial audit | AP2 non-repudiable audit trail; Interim Measures Article 14 | enforce / evidence | deny also writes a chain record before returning | `::test_t9_every_deny_leaves_a_reason_code_in_the_chain` |

## Not adopted (archived 2026-10-15)

- **R4 Replay and timeout semantics for approvals**: a predictable or replayable `approval_id` could let an old approval authorize a new call; an approval timeout must never become an automatic allow.
- **R5 Evidence truncation and disk exhaustion**: ADR-0003 guarantees tamper evidence, not tail-truncation resistance (which needs an external anchor or witness), and does not specify behavior when the disk is full (it must fail closed).
- **R7 Cost-based DoS**: unbounded pagination, oversized payloads, very long URI lists (fetch-through pulls everything) and hung upstreams — OWASP unbounded consumption.
- **R8 Upstream error messages are also an injection surface**: a pydantic validation failure echoes `input_value`; upstream errors also enter the model.

Not adopting an item is not a rejection; the archive is there so W4 can pick it up when needed.

## Relationship to other documents

- Invariants I1–I4 are in `docs/architecture.md` §6. Mapping: I1 ← T3, T4, T9; I2 ← T5; I3 ← T9 (denials also live on the chain); I4 ← the precondition for reviewing T1 and T2 after the fact (both decisions and sanitization must be recomputable).
- Related decisions: ADR-0001 (policy language), ADR-0002 (budget), ADR-0003 (evidence), ADR-0007 (denial shape), ADR-0008 (tool names), ADR-0010 (cache), ADR-0011 (interception surface), ADR-0013 (pagination), ADR-0019 / 0020 (identity and authorization composition), ADR-0023 (resources denial shape).
- The W4 adversarial suite is generated from the "adversarial test" column of this table (methodology: AgentDojo's task set + security cases + attack/defense patterns); red-team attempts are recorded in `docs/redteam-log.md`.

## Honest boundary

The gateway cannot stop an agent that bypasses it — reading files directly or calling the PSP HTTP endpoint directly does not pass through this gateway. That is a deployment boundary, not a design flaw; state it explicitly and do not claim "the gateway makes you secure."
