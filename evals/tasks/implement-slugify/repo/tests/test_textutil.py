from textutil import slugify


def test_basic():
    assert slugify("Hello, World!") == "hello-world"


def test_accents():
    assert slugify("  Crème Brûlée  ") == "creme-brulee"
