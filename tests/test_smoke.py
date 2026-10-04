"""W1 smoke test: the skeleton is wired up and importable."""

import gateway


def test_package_imports() -> None:
    assert gateway.__version__ == "0.1.0"
