import pytest

from loader import load_rows, parse_line


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("a,b,c", ["a", "b", "c"]),
        ('a,"b,c",d', ["a", "b,c", "d"]),
        ('"say ""hi""",x', ['say "hi"', "x"]),
        ('"",x', ["", "x"]),
        ("", [""]),
        ("a,", ["a", ""]),
        (",", ["", ""]),
        (' a , b ', [" a ", " b "]),
        ('"a ""b"" c"', ['a "b" c']),
        ('"multi, comma, value"', ["multi, comma, value"]),
        ("single", ["single"]),
    ],
)
def test_parse_line(line, expected):
    assert parse_line(line) == expected


def test_load_rows_with_quotes():
    text = 'name,quote\nAda,"Hello, world"\nBob,"He said ""no"""\n'
    assert load_rows(text) == [
        {"name": "Ada", "quote": "Hello, world"},
        {"name": "Bob", "quote": 'He said "no"'},
    ]


def test_blank_lines_are_skipped():
    assert load_rows("a,b\n\n1,2\n   \n3,4\n") == [{"a": "1", "b": "2"}, {"a": "3", "b": "4"}]


@pytest.mark.parametrize("text", ["", "   \n\n"])
def test_no_header_gives_no_rows(text):
    assert load_rows(text) == []


def test_header_only():
    assert load_rows("a,b\n") == []


def test_wrong_field_count_raises():
    with pytest.raises(ValueError):
        load_rows("a,b\n1,2,3\n")
    with pytest.raises(ValueError):
        load_rows("a,b\n1\n")
