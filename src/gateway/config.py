"""网关配置：上游清单、监听地址、证据文件位置。

上游用 alias 命名空间（ADR-0008）：对 agent 暴露的工具名是 `<alias>__<tool>`。
W2 的上游只有一种启动方式——stdio 子进程（ADR-0004）。
"""

from __future__ import annotations

import pathlib
import uuid
from dataclasses import dataclass, field


@dataclass(frozen=True)
class UpstreamSpec:
    """一个上游 MCP server：别名 + 启动方式。"""

    alias: str
    command: str
    args: list[str]
    env: dict[str, str] = field(default_factory=dict)


@dataclass
class GatewayConfig:
    upstreams: list[UpstreamSpec]
    host: str = "127.0.0.1"
    port: int = 8765
    path: str = "/mcp"
    evidence_path: pathlib.Path = pathlib.Path("runs/gateway-evidence.jsonl")
    run_id: str = field(default_factory=lambda: f"run-{uuid.uuid4().hex[:8]}")

    def __post_init__(self) -> None:
        aliases = [spec.alias for spec in self.upstreams]
        if len(set(aliases)) != len(aliases):
            raise ValueError(f"上游 alias 必须唯一（ADR-0008 fail fast）: {aliases}")
        if any(not alias or "__" in alias for alias in aliases):
            raise ValueError(f"alias 不能为空、也不能含 __（那是前缀分隔符）: {aliases}")

