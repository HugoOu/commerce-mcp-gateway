"""规范化摘要：一处定义，全项目共用（ADR-0014 的透传义务之一）。

键排序 + 紧凑分隔符，保证「同一逻辑报文的不同序列化」得到同一摘要——否则重放会假失败。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

__all__ = ["canonical_json", "digest", "sha256_hex"]


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


def digest(value: Any) -> str:
    """短摘要（16 位十六进制）：给日志与对照用的那种。"""
    return sha256_hex(value)[:16]


def sha256_hex(value: Any) -> str:
    """完整 sha256（64 位十六进制）。链上哈希用它——短摘要碰撞面太大，不适合做链。"""
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()

