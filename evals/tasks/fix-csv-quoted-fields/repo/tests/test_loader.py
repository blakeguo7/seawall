from loader import load_rows, parse_line


def test_simple_line():
    assert parse_line("a,b,c") == ["a", "b", "c"]


def test_quoted_comma():
    assert parse_line('a,"b,c",d') == ["a", "b,c", "d"]


def test_load_rows():
    assert load_rows("x,y\n1,2\n") == [{"x": "1", "y": "2"}]
