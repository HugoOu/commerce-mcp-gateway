"""跑薄循环 driver 的入口：把 src/ 放进 sys.path，免去安装。

用法（项目根目录）：

    # 录一条轨迹（需要 OPENAI_API_KEY / OPENAI_BASE_URL）
    uv run python scripts/run_driver.py --server merchant=src/mcp_servers/merchant.py \
        --server psp=src/mcp_servers/psp.py --task "买一份 taco" --record runs/demo.jsonl

    # 重放（不需要模型、不需要网络、不需要 mock）
    uv run python scripts/run_driver.py --replay runs/demo.jsonl
"""

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent.driver import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
