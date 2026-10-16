"""Check that a quote really appears in a text.

This is the guard behind every LLM claim in the app: a model may say "the CV mentions X",
but the claim only counts if the quoted words are found, verbatim, in the CV. The check
tolerates what extraction does to text (case, whitespace, bullets, curly quotes, line
breaks) and nothing else.
"""

import re
import unicodedata

_PUNCTUATION_MAP = str.maketrans(
    {
        "‘": "'",
        "’": "'",
        "“": '"',
        "”": '"',
        "–": "-",
        "—": "-",
        "­": "",
        "•": " ",
        "◦": " ",
        "▪": " ",
        "●": " ",
        "‣": " ",
    }
)
_EDGE_NOISE = " \t\n.,;:…\"'()[]"
MIN_QUOTE_LENGTH = 2


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).translate(_PUNCTUATION_MAP).casefold()
    return re.sub(r"\s+", " ", text).strip()


def clean_quote(quote: str) -> str:
    return normalize(quote).strip(_EDGE_NOISE)


def quote_in_text(quote: str | None, text: str) -> bool:
    """True if `quote` occurs in `text` as whole words.

    Whole-word matching matters: a CV that says "JavaScript" must not satisfy a quote of
    "Java". Quotes shorter than two characters never count."""
    if not quote:
        return False
    needle = clean_quote(quote)
    if len(needle) < MIN_QUOTE_LENGTH or not re.search(r"\w", needle):
        return False
    pattern = r"(?<!\w)" + re.escape(needle) + r"(?!\w)"
    return re.search(pattern, normalize(text)) is not None
