import importlib
import subprocess
import sys

import pytest


@pytest.mark.parametrize("first", ["shop.models", "shop.pricing"])
def test_each_module_imports_alone_in_a_fresh_interpreter(first):
    result = subprocess.run([sys.executable, "-c", f"import {first}"], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_behaviour_is_unchanged():
    from shop.models import Product
    from shop.pricing import RATES, tax_for

    assert Product("lamp", 10.0).price_with_tax() == 12.0
    assert Product("apple", 2.0, "food").price_with_tax() == 2.1
    assert Product("novel", 8.0, "books").price_with_tax() == 8.0
    assert Product("odd", 10.0, "mystery").price_with_tax() == 12.0
    assert tax_for(Product("lamp", 10.0)) == pytest.approx(2.0)
    assert set(RATES) == {"general", "food", "books"}


def test_tax_for_still_rejects_non_products():
    from shop.pricing import tax_for

    with pytest.raises(TypeError):
        tax_for("not a product")
    with pytest.raises(TypeError):
        tax_for(None)


def test_no_module_level_cycle():
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent / "shop"
    imports = {}
    for name in ("models", "pricing"):
        tree = ast.parse((root / f"{name}.py").read_text())
        imports[name] = {
            node.module
            for node in tree.body
            if isinstance(node, ast.ImportFrom) and node.module
        } | {alias.name for node in tree.body if isinstance(node, ast.Import) for alias in node.names}
    assert not ("shop.pricing" in imports["models"] and "shop.models" in imports["pricing"])
