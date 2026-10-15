"""ADR-0014 的 CI 检查：三张清单两两不交，且被拦面每一项都有归属。"""

from __future__ import annotations

from gateway.classification import CLASSIFICATION, CONSTRUCTED, PASSTHROUGH, VALIDATED, classify

INTERCEPTED_SURFACE = (
    "tools/call.arguments",  # 校验类：策略从这里读金额与商户
    "tools/call.deny_response",  # 构造类：拒绝响应
    "tools/call.approval_request",  # 构造类：审批请求
    "tools/list.filtered_result",  # 构造类：过滤后的清单
    "resources/read.uri",  # 校验类：URI 授权
    "resources/read.deny_response",  # 构造类：与「不存在」同形
    "prompts/*",  # 透传类：纳入拦截面但不改写
    "envelope.fields",  # 透传类：未知字段必须保留
)


def test_three_lists_are_pairwise_disjoint() -> None:
    assert not (CONSTRUCTED & VALIDATED)
    assert not (CONSTRUCTED & PASSTHROUGH)
    assert not (VALIDATED & PASSTHROUGH)


def test_every_intercepted_type_has_a_home() -> None:
    missing = [name for name in INTERCEPTED_SURFACE if classify(name) is None]
    assert not missing, f"这些类型没有归类（ADR-0014 要求升级看得见）：{missing}"


def test_classification_map_matches_the_lists() -> None:
    assert CLASSIFICATION["constructed"] is CONSTRUCTED
    assert CLASSIFICATION["validated"] is VALIDATED
    assert CLASSIFICATION["passthrough"] is PASSTHROUGH

