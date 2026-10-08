"""Word frequencies."""

import re
from collections import Counter

STOPWORDS = frozenset({"a", "an", "and", "the", "of", "to", "in", "is", "it"})

_WORD = re.compile(r"[A-Za-z0-9]+(?:'[A-Za-z0-9]+)*")


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
    if n <= 0:
        return []
    ignored = {word.lower() for word in stopwords}
    counts = Counter(w for w in (m.lower() for m in _WORD.findall(text)) if w not in ignored)
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return ordered[:n]
