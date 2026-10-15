"""OTel 属性映射（ADR-0005 推荐 5）。

SDK 默认已注册 `OpenTelemetryMiddleware`，每个入站请求都在一个 span 里跑；这里只做两件事：
① 统一属性命名（`commerce.gateway.*`）；② 把决策与结果挂到当前 span 上。

没有配置 exporter 时 `span.is_recording()` 为 False，`annotate` 就是空操作——W4 接 exporter
（评测要看 trace）时不需要改任何调用点。属性映射集中在本模块，是 architecture 第 8 条。
"""

from __future__ import annotations

from typing import Any

from opentelemetry import trace

PREFIX = "commerce.gateway."


def attributes(
    method: str,
    *,
    tool: str | None = None,
    upstream: str | None = None,
    decision: str | None = None,
    outcome: str | None = None,
    duration_ms: float | None = None,
) -> dict[str, Any]:
    attrs: dict[str, Any] = {f"{PREFIX}method": method}
    if tool:
        attrs[f"{PREFIX}tool"] = tool
    if upstream:
        attrs[f"{PREFIX}upstream"] = upstream
    if decision:
        attrs[f"{PREFIX}decision"] = decision
    if outcome:
        attrs[f"{PREFIX}outcome"] = outcome
    if duration_ms is not None:
        attrs[f"{PREFIX}duration_ms"] = round(duration_ms, 3)
    return attrs


def annotate(method: str, **kwargs: Any) -> None:
    """把这次调用的属性挂到当前 span；没有 exporter 时什么都不做。"""
    span = trace.get_current_span()
    if not span.is_recording():
        return
    for key, value in attributes(method, **kwargs).items():
        span.set_attribute(key, value)

