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


_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")


def _digits(token: str) -> str:
    return re.sub(r"\D", "", token)


def unsupported_numbers(text: str, allowed_text: str) -> list[str]:
    """Numbers in `text` that appear nowhere in `allowed_text` (45.000 equals 45,000).

    A made-up figure ("cut costs by 70%") is the most common way a model embellishes a
    message, and it is cheap to catch: every number the model writes must come from the CV or
    the facts it was given."""
    allowed = {_digits(t) for t in _NUMBER.findall(allowed_text)}
    found: list[str] = []
    for token in _NUMBER.findall(text):
        if _digits(token) not in allowed and token not in found:
            found.append(token)
    return found


_WORD = re.compile(r"[^\W\d_][\w+#.\-]*")
_SENTENCE_END = (".", "!", "?", ":", "\n")


def _tokens(text: str) -> list[tuple[str, int]]:
    return [(m.group().rstrip(".-"), m.start()) for m in _WORD.finditer(text)]


def unsupported_terms(text: str, allowed_text: str) -> list[str]:
    """Proper nouns and acronyms in `text` that `allowed_text` never mentions.

    A made-up tool or employer ("deploying models with Kubernetes") shows up as a capitalised
    word in the middle of a sentence, or as an acronym anywhere. Sentence-initial words and a
    lone "I" are ignored. Lower-case terms (pandas, docker) are not caught: this is a cheap
    net for the commonest embellishment, not a proof."""
    known = {token.casefold() for token, _ in _tokens(allowed_text)}
    found: list[str] = []
    for token, start in _tokens(text):
        if len(token) < 2 or not token[0].isupper():
            continue
        shouting = token.isupper()
        before = text[:start].rstrip(" \t\"'(\u00bf\u00a1")
        initial = not before or before.endswith(_SENTENCE_END)
        if initial and not shouting:
            continue
        if token.casefold() not in known and token not in found:
            found.append(token)
    return found
