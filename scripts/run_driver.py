"""跑薄循环 driver 的入口：把 src/ 放进 sys.path，免去安装。

用法（项目根目录）：

    # 录一条轨迹（需要 OPENAI_API_KEY / OPENAI_BASE_URL；OpenCode Go 还需要 x-opencode-session，见 model.py）
    uv run python scripts/run_driver.py --server merchant=src/mcp_servers/merchant.py `
        --server psp=src/mcp_servers/psp.py --task "买一份 taco" --record runs/demo.jsonl

    # 重放（不需要模型、不需要网络、不需要 mock）
    uv run python scripts/run_driver.py --replay runs/demo.jsonl
"""

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent.driver import main


def leaves(exc: BaseException):
    """把 anyio / asyncio 的 ExceptionGroup 摊平，只打真正的那条错误。"""
    if isinstance(exc, BaseExceptionGroup):
        for inner in exc.exceptions:
            yield from leaves(inner)
    else:
        yield exc


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001  顶层边界：把 ExceptionGroup 摊平
        for leaf in leaves(exc):
            print("[FATAL]", type(leaf).__name__, ":", leaf)
        raise SystemExit(1)
