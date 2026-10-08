from words import top_words


def test_basic():
    assert top_words("the cat and the dog and the bird", n=2) == [("bird", 1), ("cat", 1)]
