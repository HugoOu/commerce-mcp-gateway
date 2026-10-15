"""策略求值的测试（ADR-0001 / 0007 / 0020）——「测试当 oracle」的落点。

先写断言、再让实现到通过；边界用例（金额刚好等于上限、商户刚好在白名单）与 fail-closed
用例（字符串金额、写错的策略文件）都在这里。
"""

from __future__ import annotations

import pathlib
from datetime import UTC, datetime

import pytest

from gateway.decision import AuthorizationBasis, InterceptedRequest, Mandate
from gateway.policy import PolicyError, YamlPolicyEngine, load_policy, segment_glob

BASE_POLICY = """
version: 1
default: deny
bindings:
  psp__create_payment_intent:
    amount_field: amount
    merchant_field: merchant_id
rules:
  - id: browse
    effect: allow
    match:
      tool: "merchant__list_catalog"
  - id: any-merchant-tool
    effect: allow
    match:
      tool: "merchant__**"
  - id: pay
    effect: allow
    match:
      tool: "psp__create_payment_intent"
    constraints:
      amount_max: 30.0
      merchant_in: [m1]
  - id: refund
    effect: require_approval
    reason_code: refund_requires_approval
    match:
      tool: "merchant__request_refund"
  - id: no-admin
    effect: deny
    reason_code: admin_forbidden
    hard: true
    match:
      tool: "admin__*"
"""

MINIMAL_POLICY = """
version: 1
default: deny
bindings:
  psp__create_payment_intent:
    amount_field: amount
    merchant_field: merchant_id
rules:
  - id: pay
    effect: allow
    match:
      tool: "psp__create_payment_intent"
"""

BAD_UNKNOWN_FIELD = """
version: 1
default: deny
rules: []
extra_field: 1
"""

BAD_DEFAULT_ALLOW = """
version: 1
default: allow
"""

BAD_DENY_WITHOUT_REASON = """
version: 1
default: deny
rules:
  - id: a
    effect: deny
"""


def _engine(tmp_path: pathlib.Path, text: str = BASE_POLICY) -> YamlPolicyEngine:
    path = tmp_path / "policy.yaml"
    path.write_text(text, encoding="utf-8")
    return load_policy(path)


def _request(
    name: str,
    arguments: dict | None = None,
    method: str = "tools/call",
    now: float | None = None,
) -> InterceptedRequest:
    return InterceptedRequest(
        method=method,
        session_id="s1",
        tool_call_id="t1",
        name=name,
        arguments=arguments or {},
        now=now,
    )


def _basis(*, scopes: tuple[str, ...] = ("tools:call",), mandate: Mandate | None = None) -> AuthorizationBasis:
    return AuthorizationBasis(subject="agent-1", scopes=frozenset(scopes), audience="gw", mandate=mandate)


def test_default_deny(tmp_path: pathlib.Path) -> None:
    """没有规则命中 → deny，且 reason_code 稳定（deny-by-default）。

    两种情形都要拒：① 策略里一条规则都没有；② 有规则但都没命中——`merchant__**` 覆盖不到
    psp 的工具。第二种是最初漏掉的情形：当时拿 `merchant__cancel_checkout` 当"没人管"的例子，
    而它其实被 `merchant__**` 命中了（测试写错，实现没错）。
    """
    empty = _engine(
        tmp_path,
        """
version: 1
default: deny
rules: []
""",
    )
    assert empty.evaluate(_request("merchant__list_catalog"), _basis()).kind == "deny"

    decision = _engine(tmp_path).evaluate(_request("psp__refund_payment"), _basis())
    assert decision.kind == "deny"
    assert decision.reason_code == "policy_denied"


def test_allow_when_rule_matches(tmp_path: pathlib.Path) -> None:
    assert _engine(tmp_path).evaluate(_request("merchant__list_catalog"), _basis()).allowed


def test_segment_glob_does_not_cross_separator() -> None:
    """`*` 只在一个段内通配：`merchant__*` 不匹配三段的名字。"""
    assert segment_glob("merchant__*", "merchant__list_catalog")
    assert not segment_glob("merchant__*", "merchant__orders__get")


def test_double_star_matches_across_segments(tmp_path: pathlib.Path) -> None:
    assert segment_glob("merchant__**", "merchant__orders__get")
    assert _engine(tmp_path).evaluate(_request("merchant__orders__get"), _basis()).allowed


def test_deny_beats_allow(tmp_path: pathlib.Path) -> None:
    """最严者胜：同一请求同时命中 allow 与 deny 时取 deny（非补偿性门）。"""
    decision = _engine(tmp_path).evaluate(_request("admin__reset"), _basis())
    assert decision.kind == "deny"
    assert decision.reason_code == "admin_forbidden"
    assert decision.hard is True


def test_require_approval_beats_allow(tmp_path: pathlib.Path) -> None:
    decision = _engine(tmp_path).evaluate(_request("merchant__request_refund"), _basis())
    assert decision.kind == "require_approval"
    assert decision.reason_code == "refund_requires_approval"


def test_scope_gate_is_hard_deny(tmp_path: pathlib.Path) -> None:
    """授权门在策略之前（ADR-0020）：没有 scope 就是硬拒，策略改不了这一点。"""
    decision = _engine(tmp_path).evaluate(_request("merchant__list_catalog"), _basis(scopes=()))
    assert decision.kind == "deny"
    assert decision.reason_code == "scope_denied"
    assert decision.hard is True


def test_policy_constraint_tightens_mandate(tmp_path: pathlib.Path) -> None:
    """策略 30 与 mandate 50 取交集 → 生效上限是 30（只会更紧）。"""
    mandate = Mandate(mandate_id="m1", per_txn_max=50.0, allowed_merchants=("m1",))
    engine = _engine(tmp_path)
    over = _request("psp__create_payment_intent", {"amount": 40.0, "merchant_id": "m1"})
    within = _request("psp__create_payment_intent", {"amount": 30.0, "merchant_id": "m1"})
    assert engine.evaluate(over, _basis(mandate=mandate)).reason_code == "amount_over_limit"
    assert engine.evaluate(within, _basis(mandate=mandate)).allowed


def test_amount_over_mandate_limit(tmp_path: pathlib.Path) -> None:
    mandate = Mandate(mandate_id="m1", per_txn_max=10.0, allowed_merchants=("m1",))
    decision = _engine(tmp_path, MINIMAL_POLICY).evaluate(
        _request("psp__create_payment_intent", {"amount": 25.0, "merchant_id": "m1"}),
        _basis(mandate=mandate),
    )
    assert decision.reason_code == "amount_over_limit"


def test_merchant_not_in_allowlist(tmp_path: pathlib.Path) -> None:
    decision = _engine(tmp_path).evaluate(
        _request("psp__create_payment_intent", {"amount": 10.0, "merchant_id": "m2"}),
        _basis(),
    )
    assert decision.reason_code == "merchant_not_allowed"


def test_string_amount_fails_closed(tmp_path: pathlib.Path) -> None:
    """参数是未校验的原始值：字符串金额不算数字，必须拒（fail-closed）。"""
    decision = _engine(tmp_path).evaluate(
        _request("psp__create_payment_intent", {"amount": "9.71", "merchant_id": "m1"}),
        _basis(),
    )
    assert decision.reason_code == "amount_unreadable"


def test_mandate_expiry_needs_explicit_now(tmp_path: pathlib.Path) -> None:
    """纯函数：不传 now 就不做过期判定；传了才判——不偷读时钟。"""
    mandate = Mandate(mandate_id="m1", expires_at="2026-01-01T00:00:00+00:00")
    later = datetime(2026, 6, 1, tzinfo=UTC).timestamp()
    request = _request("merchant__list_catalog")
    assert _engine(tmp_path).evaluate(request, _basis(mandate=mandate)).allowed
    expired = _request("merchant__list_catalog", now=later)
    assert _engine(tmp_path).evaluate(expired, _basis(mandate=mandate)).reason_code == "mandate_expired"


@pytest.mark.parametrize("text", [BAD_UNKNOWN_FIELD, BAD_DEFAULT_ALLOW, BAD_DENY_WITHOUT_REASON])
def test_policy_file_errors_fail_loudly(tmp_path: pathlib.Path, text: str) -> None:
    """策略文件写错要立刻失败——静默忽略一个字段等于静默放宽一条边界。"""
    path = tmp_path / "policy.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises((PolicyError, ValueError)):
        load_policy(path)


def test_evaluation_is_pure_and_digest_stable(tmp_path: pathlib.Path) -> None:
    """同一输入两次求值逐字相同；同一文件两次加载 digest 相同（重放前置条件）。"""
    request = _request("psp__create_payment_intent", {"amount": 10.0, "merchant_id": "m1"})
    first = _engine(tmp_path).evaluate(request, _basis())
    second = _engine(tmp_path).evaluate(request, _basis())
    assert first == second
    assert _engine(tmp_path).digest == _engine(tmp_path).digest

