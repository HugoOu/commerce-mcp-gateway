"""I1 的结构契约（architecture 第 2 条 / ADR-0005）：上游客户端只能出现在 upstream.py。

用 AST 而不是 import-linter：零新增依赖，`uv run pytest -q` 就能跑。将来要更细的分层规则
（例如 policy 不得 import enforce）时再引入 import-linter。
"""

from __future__ import annotations

import ast
import importlib
import pathlib

GATEWAY = pathlib.Path(__file__).resolve().parents[1] / "src" / "gateway"
ALLOWED_UPSTREAM_CLIENT = {"upstream.py"}


def _imports(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            found.add(node.module or "")
    return found


def test_only_upstream_imports_mcp_client():
    offenders = {}
    for path in sorted(GATEWAY.glob("*.py")):
        hits = {name for name in _imports(path) if name.startswith("mcp.client")}
        if hits and path.name not in ALLOWED_UPSTREAM_CLIENT:
            offenders[path.name] = sorted(hits)
    assert not offenders, f"只有 upstream.py 可以持有上游客户端（I1 的结构），违反者：{offenders}"


def test_frontdoor_keeps_no_decision_logic():
    """前门只能把请求交给执行点：它不该直接碰账本或上游。"""
    imports = _imports(GATEWAY / "frontdoor.py")
    forbidden = {name for name in imports if name.endswith(("upstream", "evidence"))}
    assert not forbidden, f"frontdoor.py 不该 import {forbidden}"


def test_gateway_modules_import():
    """冒烟：网关各模块能被 import（抓拼写与导入错误，不需要网络）。"""
    for name in (
        "gateway.config",
        "gateway.decision",
        "gateway.evidence",
        "gateway.policy",
        "gateway.budget",
        "gateway.classification",
        "gateway.telemetry",
        "gateway.upstream",
        "gateway.enforce",
        "gateway.frontdoor",
        "gateway.app",
    ):
        importlib.import_module(name)

