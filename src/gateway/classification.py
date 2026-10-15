"""类型三分类清单（ADR-0014）：把「升级」写在 diff 里，而不是靠记忆。

- `CONSTRUCTED`：网关要**自己构造**的报文 → 需要 golden 测试 + 用 pydantic 反向校验。
- `VALIDATED`：网关要**读字段做判定**的 → 需要 negative 测试与边界用例。
- `PASSTHROUGH`：只透传 → 需要 round-trip 与摘要稳定性测试。

三张清单必须**两两不交**，且被拦面（ADR-0011：`tools/*`、`resources/*`、`prompts/get`）
每一项都要有归属——`tests/test_classification.py` 把这两条钉成 CI 检查。
升级一个类型（例如 ADR-0011 把 `resources/list` 从透传升为构造）时，改动必须落在这里。
"""

from __future__ import annotations

CONSTRUCTED: frozenset[str] = frozenset(
    {
        "tools/call.deny_response",  # ADR-0007 第 2 层：CallToolResult(is_error=True) + reason_code
        "tools/call.approval_request",  # ADR-0006：resultType=input_required + requestState
        "tools/list.filtered_result",  # ADR-0010 / 0011：按授权指纹过滤后的清单
        "resources/list.filtered_result",  # ADR-0013（future work；未实现时 fail-closed）
        "resources/list.next_cursor",  # ADR-0013：自签游标
        "resources/read.deny_response",  # ADR-0023：-32602 与「不存在」同形
        "evidence.entry",  # ADR-0003：链上记录
    }
)

VALIDATED: frozenset[str] = frozenset(
    {
        "tools/call.arguments",  # 金额、商户、品类——ADR-0001 的算子输入
        "resources/read.uri",  # ADR-0023：URI 授权过滤
        "content.text",  # sanitize：注入筛查 / PII 脱敏 / audience 过滤
        "authorization.token",  # ADR-0019 / 0020：audience 绑定、scope 交集
    }
)

PASSTHROUGH: frozenset[str] = frozenset(
    {
        "server/discover.response",  # ADR-0018：不翻译，原样转达
        "prompts/*",  # ADR-0011：纳入拦截面，但不改写内容
        "resources/templates/list",
        "envelope.fields",  # ADR-0014：未知字段必须保留
        "notifications/*",  # W3 不实现通知过滤（留 W4 的对抗套件）
    }
)

CLASSIFICATION: dict[str, frozenset[str]] = {
    "constructed": CONSTRUCTED,
    "validated": VALIDATED,
    "passthrough": PASSTHROUGH,
}


def classify(name: str) -> str | None:
    """返回某个类型属于哪一类；不在清单里返回 `None`。"""
    for kind, names in CLASSIFICATION.items():
        if name in names:
            return kind
    return None

