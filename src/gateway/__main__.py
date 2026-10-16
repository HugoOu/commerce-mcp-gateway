"""`python -m gateway` 的入口——真正的实现在 `cli.py`。

这一层刻意只有两行：让测试可以 `from gateway.cli import main` 直接调，不必经过 `runpy`。
"""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())

