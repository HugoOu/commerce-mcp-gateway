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
    """**真的改了内容**的项（PII 脱敏）。注入命中**不算**——那只是标记，内容一个字没动。"""
    pii_hits: tuple[str, ...] = ()
    """检测到的 PII（«redact=False» 时也会记——那是「只标记不遮盖」那一档，见 §2 的筛查三档）。"""
    injection_hits: tuple[str, ...] = ()
    """疑似注入的命中项（只标记、不改内容）。"""
    flagged_injection: bool = False
    dropped_blocks: int = 0

    @property
    def changed(self) -> bool:
        """内容**被改过**吗（脱敏或丢块）——证据里的 `sanitized` 就是它。

        注意它**不包含**注入命中：命中注入时内容原样交给模型（净化不改决策，删了就没人能复算）。
        这条界线以前是糊的——`redactions` 里混进了 `injection:*`，于是「只命中注入」也会被记成
        `sanitized=True`。2026-10-16 的 T2 用例把它照出来了。
        """
        return bool(self.redactions) or self.dropped_blocks > 0


@dataclass
class Sanitizer:
    enabled: bool = True
    redact: bool = True
    """是否**真的替换**命中 PII 的文本（False = 只标记不遮盖，曲线横轴的中间那档）。"""
    audience: str = "assistant"
    """只有 `audience` 含这一项的块才交给模型（规格的 `Annotations.audience` 语义）。"""

    def text(self, value: str) -> tuple[str, list[str], list[str], bool]:
        """返回 (净化后的文本, 脱敏项, 注入命中项, 是否疑似注入)。

        两个列表**刻意分开**：脱敏项代表「内容被改了」，注入命中代表「内容没改，只是可疑」。
        混在一起会让证据说不清「到底动了没有」。
        """
        if not self.enabled:
            return value, [], [], False
        pii: list[str] = []
        injection: list[str] = []
        for name, pattern in INJECTION_PATTERNS:
            if pattern.search(value):
                injection.append(f"injection:{name}")
        for name, pattern in PII_PATTERNS:
            if pattern.search(value):
                pii.append(f"pii:{name}")
                if self.redact:
                    value = pattern.sub(REDACTION, value)
        return value, pii, injection, bool(injection)

    def apply(self, result: types.CallToolResult) -> tuple[types.CallToolResult, SanitizeReport]:
        """净化工具结果。非文本块原样保留（W3 只处理文本）。"""
        if not self.enabled:
            return result, SanitizeReport()
        redactions: list[str] = []
        pii_hits: list[str] = []
        injections: list[str] = []
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
                cleaned, pii, injection, suspicious = self.text(text)
                pii_hits.extend(pii)
                if self.redact:
                    redactions.extend(pii)
                injections.extend(injection)
                flagged = flagged or suspicious
                block = block.model_copy(update={"text": cleaned})
            kept.append(block)
        report = SanitizeReport(
            redactions=tuple(sorted(set(redactions))),
            pii_hits=tuple(sorted(set(pii_hits))),
            injection_hits=tuple(sorted(set(injections))),
            flagged_injection=flagged,
            dropped_blocks=dropped,
        )
        return result.model_copy(update={"content": kept}), report

