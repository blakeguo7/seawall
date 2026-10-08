from mdtable import render_table


def test_example():
    out = render_table(["name", "qty"], [["apple", 3], ["fig", 12]])
    assert out.splitlines()[0] == "| name  | qty |"
