"""Word frequencies."""

STOPWORDS = frozenset({"a", "an", "and", "the", "of", "to", "in", "is", "it"})


def top_words(text, n=3, stopwords=STOPWORDS):
    """The n most frequent words of text as (word, count) tuples.

    * words are made of letters, digits and apostrophes inside a word ("don't" is one word);
      everything else separates words; leading and trailing apostrophes are not part of a word
      ("'quoted'" -> "quoted")
    * matching ignores case: "The" and "the" are the same word, reported in lower case
    * words in `stopwords` (compared in lower case) are ignored
    * the result is sorted by count, highest first; words with the same count are sorted
      alphabetically
    * fewer than n distinct words give a shorter list; n <= 0 gives []
    """
    raise NotImplementedError
