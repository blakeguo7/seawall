"""Text helpers."""

import re
import unicodedata


def slugify(text, max_length=None):
    """Turn text into a URL slug.

    * lower-case everything
    * strip accents: "Crème Brûlée" -> "creme-brulee" (decompose with unicodedata and drop the
      combining marks)
    * every run of characters that are not ASCII letters or digits becomes a single "-"
    * no leading or trailing "-"
    * text with nothing usable gives ""
    * max_length (if given) cuts the slug to at most that many characters, preferring to cut at a
      "-" so a word is not split, and never leaves a trailing "-": slugify("hello world foo", 11)
      -> "hello-world"; when the first word alone is longer than max_length, cut inside it.
    """
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    slug = re.sub(r"[^a-z0-9]+", "-", stripped.lower()).strip("-")
    if max_length is not None and len(slug) > max_length:
        cut = slug[:max_length]
        if slug[max_length] != "-" and "-" in cut:
            cut = cut[: cut.rindex("-")]
        slug = cut.strip("-")
    return slug
