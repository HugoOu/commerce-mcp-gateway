"""策略求值实现（ADR-0001 / 0007 / 0020）——AI 起草的「第二版」，等你的版本做 diff。

求值顺序（每一步都有出处）：

1. **授权门**（ADR-0020）：方法要求的 scope 不在 `basis.scopes` 里 → 硬拒绝 `scope_denied`。
   策略**只能收紧授权**，所以这一步在策略之前。
2. **规则命中**：所有命中规则一起看，**最严者胜**（deny > require_approval > allow）。
   这就是 `docs/evaluation.md` 里那条「非补偿性门」：不给分数、不做加权，命中即生效。
3. **约束检查**（只对 allow）：策略约束与 mandate 约束**取交集**，永远只会更紧；
   金额与商户从参数里读（ADR-0014 的「校验类」），读不到就 fail-closed。
4. **默认 deny**：没有任何 allow 命中 → `policy_denied`。

纯函数（architecture 第 4 条）：不读时钟、不读随机。mandate 的过期判定要 `request.now`，
不传就**不做**过期判定（而不是偷偷读 `time.time()`）。
"""

from __future__ import annotations

import fnmatch
import pathlib
from datetime import datetime
from typing import Any

import yaml

from ..decision import AuthorizationBasis, Decision, InterceptedRequest, Mandate
from ..hashing import digest
from .schema import ArgMatcher, Binding, Constraints, Match, PolicyError, PolicyFile, Rule

__all__ = ["YamlPolicyEngine", "load_policy", "segment_glob"]

TOOL_SEP = "__"
URI_SEP = "/"

SCOPE_DENIED = "scope_denied"
POLICY_DENIED = "policy_denied"
AMOUNT_OVER_LIMIT = "amount_over_limit"
AMOUNT_UNREADABLE = "amount_unreadable"
MERCHANT_NOT_ALLOWED = "merchant_not_allowed"
MERCHANT_MISSING = "merchant_missing"
CATEGORY_NOT_ALLOWED = "category_not_allowed"
MANDATE_EXPIRED = "mandate_expired"
MANDATE_INVALID = "mandate_invalid"


def segment_glob(pattern: str, value: str, *, sep: str = TOOL_SEP) -> bool:
    """段感知 glob（ADR-0001）。

    - `*` 只在**一个段内**通配，不跨段：`merchant__*` 匹配 `merchant__list_catalog`，
      但**不**匹配 `merchant__orders__get`（段数不同）；
    - 单独的 `**` 段匹配零个或多个段：`merchant__**` 匹配上面两个；
    - 其余段必须字面相等。

    工具名用 `__` 分段（ADR-0008 的前缀就是分段符），URI 用 `/` 分段。
    """
    pattern_segments = pattern.split(sep)
    value_segments = value.split(sep)
    table = [[False] * (len(value_segments) + 1) for _ in range(len(pattern_segments) + 1)]
    table[0][0] = True
    for i, segment in enumerate(pattern_segments, start=1):
        for j in range(len(value_segments) + 1):
            if segment == "**":
                table[i][j] = table[i - 1][j] or (j > 0 and table[i][j - 1])
            elif j > 0:
                table[i][j] = table[i - 1][j - 1] and fnmatch.fnmatchcase(value_segments[j - 1], segment)
    return table[len(pattern_segments)][len(value_segments)]


def _number(value: Any) -> float | None:
    """只认真正的数字：字符串数字**不算**（ADR-0014 的校验类要自己判类型）。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    return None


def _timestamp(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return None


def _arg_matches(matcher: ArgMatcher, value: Any) -> bool:
    if matcher.exists is not None and (value is not None) != matcher.exists:
        return False
    if matcher.eq is not None and value != matcher.eq:
        return False
    if matcher.in_ is not None and value not in matcher.in_:
        return False
    if matcher.max is not None:
        number = _number(value)
        if number is None or number > matcher.max:
            return False
    if matcher.min is not None:
        number = _number(value)
        if number is None or number < matcher.min:
            return False
    if matcher.glob is not None:
        if not isinstance(value, str) or not fnmatch.fnmatchcase(value, matcher.glob):
            return False
    return True


def _matches(match: Match, request: InterceptedRequest) -> bool:
    if match.method is not None and match.method != request.method:
        return False
    if match.tool is not None:
        if request.method != "tools/call" or request.name is None:
            return False
        if not segment_glob(match.tool, request.name, sep=TOOL_SEP):
            return False
    if match.uri is not None:
        if request.method != "resources/read" or request.name is None:
            return False
        if not segment_glob(match.uri, request.name, sep=URI_SEP):
            return False
    return all(_arg_matches(matcher, request.arguments.get(field)) for field, matcher in match.args.items())


def _mandate_constraints(mandate: Mandate | None) -> Constraints | None:
    if mandate is None:
        return None
    return Constraints(
        amount_max=mandate.per_txn_max,
        merchant_in=list(mandate.allowed_merchants),
        category_in=list(mandate.allowed_categories),
    )


def _tightest(*groups: Constraints | None) -> Constraints:
    """约束取交集：金额取更小的上限，白名单取交集（ADR-0020：只会更紧）。"""
    amount_max: float | None = None
    merchants: set[str] | None = None
    categories: set[str] | None = None
    for group in groups:
        if group is None:
            continue
        if group.amount_max is not None:
            amount_max = group.amount_max if amount_max is None else min(amount_max, group.amount_max)
        if group.merchant_in:
            current = set(group.merchant_in)
            merchants = current if merchants is None else merchants & current
        if group.category_in:
            current = set(group.category_in)
            categories = current if categories is None else categories & current
    return Constraints(
        amount_max=amount_max,
        merchant_in=sorted(merchants or []),
        category_in=sorted(categories or []),
    )


class YamlPolicyEngine:
    """YAML 策略引擎：纯函数、固定算子、最严者胜。"""

    def __init__(self, policy: PolicyFile) -> None:
        self.policy = policy

    @property
    def digest(self) -> str:
        """策略摘要：进证据，是重放前置条件的一部分（ADR-0003 / evaluation.md）。"""
        return digest(self.policy.model_dump(by_alias=True, mode="json"))

    def evaluate(self, request: InterceptedRequest, basis: AuthorizationBasis) -> Decision:
        required = self.policy.required_scopes.get(request.method)
        if required is not None and required not in basis.scopes:
            return Decision(kind="deny", reason_code=SCOPE_DENIED, hard=True)

        hits = [rule for rule in self.policy.rules if _matches(rule.match, request)]
        for effect in ("deny", "require_approval"):
            rule = next((candidate for candidate in hits if candidate.effect == effect), None)
            if rule is not None:
                return Decision(kind=effect, reason_code=rule.reason_code or POLICY_DENIED, hard=rule.hard)

        allow_rule = next((candidate for candidate in hits if candidate.effect == "allow"), None)
        if allow_rule is None:
            return Decision(kind="deny", reason_code=POLICY_DENIED)

        return self._check_constraints(allow_rule, request, basis)

    def _check_constraints(
        self, rule: Rule, request: InterceptedRequest, basis: AuthorizationBasis
    ) -> Decision:
        mandate = basis.mandate
        binding = self.policy.bindings.get(request.name or "", Binding())

        # 纯函数：没有 `now` 输入就不做过期判定（不偷读时钟）；有输入就必须判。
        if mandate is not None and mandate.expires_at is not None and request.now is not None:
            expires_at = _timestamp(mandate.expires_at)
            if expires_at is None:
                return Decision(kind="deny", reason_code=MANDATE_INVALID, hard=True)
            if expires_at <= request.now:
                return Decision(kind="deny", reason_code=MANDATE_EXPIRED)

        amount: float | None = None
        if binding.amount_field is not None:
            amount = _number(request.arguments.get(binding.amount_field))
            if amount is None:
                return Decision(kind="deny", reason_code=AMOUNT_UNREADABLE)

        merchant: str | None = None
        if binding.merchant_field is not None:
            raw = request.arguments.get(binding.merchant_field)
            if not isinstance(raw, str):
                return Decision(kind="deny", reason_code=MERCHANT_MISSING)
            merchant = raw

        category: str | None = None
        if binding.category_field is not None:
            raw = request.arguments.get(binding.category_field)
            category = raw if isinstance(raw, str) else None

        constraints = _tightest(rule.constraints, _mandate_constraints(mandate))
        if amount is not None and constraints.amount_max is not None and amount > constraints.amount_max:
            return Decision(kind="deny", reason_code=AMOUNT_OVER_LIMIT)
        if merchant is not None and constraints.merchant_in and merchant not in constraints.merchant_in:
            return Decision(kind="deny", reason_code=MERCHANT_NOT_ALLOWED)
        if category is not None and constraints.category_in and category not in constraints.category_in:
            return Decision(kind="deny", reason_code=CATEGORY_NOT_ALLOWED)
        if (
            mandate is not None
            and mandate.budget_total is not None
            and amount is not None
            and amount > mandate.budget_total
        ):
            # 单笔就已超过总预算 → 直接拒；**累计**预算由 budget/ 负责（ADR-0002）
            return Decision(kind="deny", reason_code=AMOUNT_OVER_LIMIT)

        return Decision(kind="allow")


def load_policy(path: pathlib.Path) -> YamlPolicyEngine:
    """读 YAML 策略文件。任何解析 / 校验错误都抛 `PolicyError`（加载即失败）。"""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise PolicyError(f"策略文件读不了：{path}（{exc}）") from exc
    except yaml.YAMLError as exc:
        raise PolicyError(f"策略文件不是合法 YAML：{path}（{exc}）") from exc
    if raw is None:
        raise PolicyError(f"策略文件是空的：{path}")
    if not isinstance(raw, dict):
        raise PolicyError(f"策略文件顶层必须是映射：{path}")
    return YamlPolicyEngine(PolicyFile.model_validate(raw))

