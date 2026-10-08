import pytest

from mdtable import render_table


def test_docstring_example():
    out = render_table(["name", "qty"], [["apple", 3], ["fig", 12]])
    assert out == "\n".join(
        [
            "| name  | qty |",
            "| ----- | --: |",
            "| apple |   3 |",
            "| fig   |  12 |",
        ]
    )


def test_minimum_width_is_three():
    out = render_table(["a"], [["b"]])
    assert out == "\n".join(["| a   |", "| --- |", "| b   |"])


def test_numeric_column_with_header_wider_than_values():
    out = render_table(["amount"], [[1], [22.5]])
    assert out == "\n".join(
        [
            "| amount |",
            "| -----: |",
            "|      1 |",
            "|   22.5 |",
        ]
    )


def test_mixed_column_is_text():
    out = render_table(["v"], [[1], ["x"]])
    assert out == "\n".join(["| v   |", "| --- |", "| 1   |", "| x   |"])


def test_bools_are_text():
    out = render_table(["ok"], [[True], [False]])
    assert out.splitlines()[1] == "| ----- |"
    assert out.splitlines()[2] == "| True  |"


def test_none_is_empty_and_short_rows_are_padded():
    out = render_table(["a", "b"], [[None, "x"], ["y"]])
    assert out == "\n".join(
        [
            "| a   | b   |",
            "| --- | --- |",
            "|     | x   |",
            "| y   |     |",
        ]
    )


def test_numeric_with_none_is_text():
    out = render_table(["n"], [[1], [None]])
    assert out.splitlines()[1] == "| --- |"


def test_too_many_cells_raises():
    with pytest.raises(ValueError):
        render_table(["a"], [["x", "y"]])


def test_no_rows():
    assert render_table(["abc", "d"], []) == "\n".join(["| abc | d   |", "| --- | --- |"])


def test_no_trailing_newline():
    assert not render_table(["a"], [["b"]]).endswith("\n")
