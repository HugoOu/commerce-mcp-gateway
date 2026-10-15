"""规范化摘要：一处定义，全项目共用（ADR-0014 的透传义务之一）。

键排序 + 紧凑分隔符，保证「同一逻辑报文的不同序列化」得到同一摘要——否则重放会假失败。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

__all__ = ["canonical_json", "digest"]


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()[:16]

