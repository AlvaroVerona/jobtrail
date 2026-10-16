import json
import re
from collections.abc import Iterator
from typing import Any

_PYTHON_LITERALS = {"None": "null", "True": "true", "False": "false"}


def _iter_outside_strings(text: str, start: int = 0) -> Iterator[tuple[int, str, bool]]:
    """Yields (index, char, in_string) for each character in text[start:],
    tracking JSON string-literal boundaries (quote/escape state) once. Both
    extract_json's brace matcher and _normalize_python_literals below need
    this exact same "am I inside a quoted string right now" tracking — this
    is the shared implementation so a future fix (e.g. \\u escapes) only has
    to happen in one place instead of two loops silently drifting apart."""

    in_string = False
    escaped = False

    for i in range(start, len(text)):
        char = text[i]
        yield i, char, in_string

        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True


def _normalize_python_literals(json_str: str) -> str:
    """Local models (observed live, repeatedly, with llama3.1) sometimes emit
    Python's None/True/False instead of JSON's null/true/false — most often
    in a null citation field, e.g. {"chunk_id": None}. This isn't a one-off
    fluke: seen 3/3 retries in a row for the same request, so retrying alone
    never recovers from it. Replaces bare tokens outside string literals
    only, so real content that happens to contain these words (e.g. a
    statement mentioning "None available") is left untouched."""

    out: list[str] = []
    n = len(json_str)
    chars = _iter_outside_strings(json_str)

    for i, char, in_string in chars:
        if in_string:
            out.append(char)
            continue

        matched_replacement = None
        matched_len = 0
        for literal, replacement in _PYTHON_LITERALS.items():
            end = i + len(literal)
            if json_str[i:end] != literal:
                continue
            before_ok = i == 0 or not (json_str[i - 1].isalnum() or json_str[i - 1] == "_")
            after_ok = end >= n or not (json_str[end].isalnum() or json_str[end] == "_")
            if before_ok and after_ok:
                matched_replacement, matched_len = replacement, len(literal)
                break

        if matched_replacement is None:
            out.append(char)
            continue

        out.append(matched_replacement)
        for _ in range(matched_len - 1):  # the for-loop above already consumed the first character
            next(chars, None)

    return "".join(out)


def extract_json(text: str) -> dict[str, Any]:
    """Extracts the first JSON object from an LLM response. Tolerates
    markdown fences, leading/trailing prose, and trailing commas."""

    if not text or not isinstance(text, str):
        raise ValueError("Empty LLM response")

    cleaned = text.replace("```json", "").replace("```", "").strip()

    start = cleaned.find("{")
    if start == -1:
        raise ValueError(f"No JSON found in response:\n{cleaned}")

    depth = 0
    end = None

    for i, char, in_string in _iter_outside_strings(cleaned, start):
        if in_string:
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                end = i
                break

    if end is None:
        raise ValueError(f"Incomplete or malformed JSON:\n{cleaned}")

    json_str = cleaned[start : end + 1]
    json_str = _normalize_python_literals(json_str)
    json_str = re.sub(r",\s*}", "}", json_str)
    json_str = re.sub(r",\s*]", "]", json_str)

    try:
        return json.loads(json_str)
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON extracted from LLM:\n{json_str}") from e
