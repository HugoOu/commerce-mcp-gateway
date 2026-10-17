"""测试的全局准备：把 `eval/` 加进 `sys.path`。

统计脚手架与实验模块住在 `eval/`（它们不是产品包，所以不进 `src/`），而 pytest 只会把
`tests/` 插进 `sys.path`。这里补上这一条，测试就能直接 `from stats import ...`。

命名提醒：统计模块叫 `stats.py` 而不是 `harness.py`——后者已经被 `tests/harness.py` 占用，
同名会让 `import harness` 变成掷骰子。
"""

from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
EVAL = ROOT / "eval"
if str(EVAL) not in sys.path:
    sys.path.insert(0, str(EVAL))

