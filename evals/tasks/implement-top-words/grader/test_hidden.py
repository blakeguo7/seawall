import pytest

from words import STOPWORDS, top_words


def test_counts_and_orders_by_frequency_then_alphabet():
    text = "pear apple pear fig apple pear"
    assert top_words(text, n=3) == [("pear", 3), ("apple", 2), ("fig", 1)]


def test_ties_are_alphabetical():
    assert top_words("pear fig apple", n=3) == [("apple", 1), ("fig", 1), ("pear", 1)]


def test_case_insensitive_and_reported_lower_case():
    assert top_words("Go go GO Stop", n=2) == [("go", 3), ("stop", 1)]


def test_punctuation_separates_words():
    assert top_words("red, red; green! red? (blue)", n=4) == [("red", 3), ("blue", 1), ("green", 1)]


def test_apostrophes_inside_words_are_kept():
    assert top_words("don't stop, don't quit", n=2) == [("don't", 2), ("quit", 1)]


def test_outer_apostrophes_are_dropped():
    assert top_words("'quoted' quoted", n=1) == [("quoted", 2)]


def test_digits_are_word_characters():
    assert top_words("py3 py3 py2", n=2) == [("py3", 2), ("py2", 1)]


def test_stopwords_are_ignored_by_default():
    assert top_words("the cat and the dog", n=5) == [("cat", 1), ("dog", 1)]


def test_custom_stopwords_replace_the_default():
    assert top_words("the cat the dog", n=2, stopwords={"cat"}) == [("the", 2), ("dog", 1)]


def test_stopwords_compared_in_lower_case():
    assert top_words("The THE Cat", n=2, stopwords={"the"}) == [("cat", 1)]


@pytest.mark.parametrize("n", [0, -3])
def test_non_positive_n(n):
    assert top_words("a b c", n=n) == []


def test_fewer_words_than_n_and_empty_text():
    assert top_words("solo", n=5) == [("solo", 1)]
    assert top_words("", n=3) == []
    assert top_words("   ,,, ", n=3) == []


def test_default_stopwords_constant_is_unchanged():
    assert "the" in STOPWORDS and isinstance(STOPWORDS, frozenset)
