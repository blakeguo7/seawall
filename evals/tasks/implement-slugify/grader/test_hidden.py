import pytest

from textutil import slugify


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Hello, World!", "hello-world"),
        ("  Crème Brûlée  ", "creme-brulee"),
        ("a---b", "a-b"),
        ("Ünïcödé", "unicode"),
        ("100% sure", "100-sure"),
        ("   ", ""),
        ("!!!", ""),
        ("", ""),
        ("snake_case_name", "snake-case-name"),
        ("Tab\tand\nnewline", "tab-and-newline"),
        ("ÀÉÎÕÜ", "aeiou"),
        ("日本語", ""),
    ],
)
def test_slugify(text, expected):
    assert slugify(text) == expected


@pytest.mark.parametrize(
    ("text", "limit", "expected"),
    [
        ("hello world foo", 11, "hello-world"),
        ("hello world foo", 15, "hello-world-foo"),
        ("hello world foo", 100, "hello-world-foo"),
        ("hello world foo", 12, "hello-world"),
        ("supercalifragilistic", 5, "super"),
        ("ab cd", 3, "ab"),
        ("ab cd", 2, "ab"),
        ("ab cd", 1, "a"),
    ],
)
def test_max_length(text, limit, expected):
    result = slugify(text, max_length=limit)
    assert result == expected
    assert len(result) <= limit
    assert not result.endswith("-")
