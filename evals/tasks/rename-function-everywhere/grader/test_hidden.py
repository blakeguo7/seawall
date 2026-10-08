from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def test_new_name_exists_and_works():
    from pricing import compute_total

    assert compute_total([("pen", 1.5, 2), ("book", 10, 1)]) == 13.0
    assert compute_total([("pen", 10, 1)], tax_rate=0.5) == 15.0
    assert compute_total([]) == 0


def test_old_name_is_gone_from_the_module():
    import pricing

    assert not hasattr(pricing, "calc_total")


def test_dependents_still_work():
    from invoice import make_invoice
    from report import revenue

    assert make_invoice("Ada", [("pen", 10, 1)], tax_rate=0.1)["total"] == 11.0
    assert revenue([[("a", 2, 3)], [("b", 4, 1)]]) == 10.0


@pytest.mark.parametrize("suffix", ["*.py", "*.md"])
def test_no_reference_to_the_old_name_remains(suffix):
    offenders = [
        str(path.relative_to(ROOT))
        for path in ROOT.rglob(suffix)
        if "_grader" not in path.parts and "calc_total" in path.read_text()
    ]
    assert offenders == []


def test_tests_use_the_new_name():
    text = (ROOT / "tests" / "test_pricing.py").read_text()
    assert "compute_total" in text
