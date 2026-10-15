"""结果净化（最小版）：注入筛查 + PII 脱敏 + audience 过滤——AI 起草，见 ADR-0026。

三条纪律（都是别的决策推导出来的，不是偏好）：

1. **净化不改变决策**：它在决策之后运行，由注入驱动的调用照样要过执行点（I1 / 威胁 T2）；
2. 命中注入只**标记**，不因此放行或拒绝——净化是结果处理，不是判定；
3. 只动 `content` 里的文本与不该给模型看的块，**不动信封字段**（ADR-0014 的透传义务）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import mcp_types as types

__all__ = ["SanitizeReport", "Sanitizer"]

INJECTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("ignore_previous", re.compile(r"ignore\s+(all\s+)?previous\s+instructions", re.IGNORECASE)),
    ("system_note", re.compile(r"\bsystem\s+note\b", re.IGNORECASE)),
    ("role_hijack", re.compile(r"\byou\s+are\s+now\b|\bact\s+as\b", re.IGNORECASE)),
    ("exfiltrate", re.compile(r"\b(send|copy|email|leak)\b.{0,40}\b(address|email|credential|token)\b", re.IGNORECASE)),
)
"""注入筛查是**关键词级**的，不是分类器：它只负责标记与降低噪声，不负责判定（威胁 T2）。"""

PII_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("email", re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")),
    ("card_number", re.compile(r"\b(?:\d[ -]?){13,19}\b")),
    ("cn_mobile", re.compile(r"\b1[3-9]\d{9}\b")),
)

REDACTION = "[redacted]"


@dataclass(frozen=True)
class SanitizeReport:
    """这次净化做了什么——进证据，但不进线上响应（ADR-0003 的脱敏标记）。"""

    redactions: tuple[str, ...] = ()
    flagged_injection: bool = False
    dropped_blocks: int = 0

    @property
    def changed(self) -> bool:
        return bool(self.redactions) or self.dropped_blocks > 0


@dataclass
class Sanitizer:
    enabled: bool = True
    audience: str = "assistant"
    """只有 `audience` 含这一项的块才交给模型（规格的 `Annotations.audience` 语义）。"""

    def text(self, value: str) -> tuple[str, list[str], bool]:
        """返回 (净化后的文本, 命中项, 是否疑似注入)。"""
        if not self.enabled:
            return value, [], False
        hits: list[str] = []
        flagged = False
        for name, pattern in INJECTION_PATTERNS:
            if pattern.search(value):
                hits.append(f"injection:{name}")
                flagged = True
        for name, pattern in PII_PATTERNS:
            if pattern.search(value):
                hits.append(f"pii:{name}")
                value = pattern.sub(REDACTION, value)
        return value, hits, flagged

    def apply(self, result: types.CallToolResult) -> tuple[types.CallToolResult, SanitizeReport]:
        """净化工具结果。非文本块原样保留（W3 只处理文本）。"""
        if not self.enabled:
            return result, SanitizeReport()
        hits: list[str] = []
        flagged = False
        kept: list[types.ContentBlock] = []
        dropped = 0
        for block in result.content:
            audience = getattr(getattr(block, "annotations", None), "audience", None)
            if audience is not None and self.audience not in audience:
                dropped += 1
                continue
            text = getattr(block, "text", None)
            if isinstance(text, str):
                cleaned, found, suspicious = self.text(text)
                hits.extend(found)
                flagged = flagged or suspicious
                block = block.model_copy(update={"text": cleaned})
            kept.append(block)
        report = SanitizeReport(redactions=tuple(sorted(set(hits))), flagged_injection=flagged, dropped_blocks=dropped)
        return result.model_copy(update={"content": kept}), report

