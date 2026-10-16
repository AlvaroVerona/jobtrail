import logging
from collections.abc import Callable

from jobtrail.llm.base import LLMClient, LLMError
from jobtrail.llm.json_utils import extract_json

logger = logging.getLogger(__name__)


def generate_json_with_retry[T](
    llm: LLMClient,
    *,
    label: str,
    max_retries: int,
    build_prompt: Callable[[str], str],
    parse: Callable[[dict], T],
    schema: dict | None = None,
) -> T:
    """Ask for JSON, validate it, and on any failure retry with the error fed back.

    `build_prompt(error_block)` formats one attempt's prompt, embedding the (possibly empty)
    note about what went wrong last time. `parse(data)` must raise on anything unusable:
    a schema mismatch is a reason to retry, and so is a quote that is not in the source.
    Raises LLMError when every attempt fails."""
    last_error = ""
    for attempt in range(max_retries + 1):
        logger.info("%s attempt %d/%d", label, attempt + 1, max_retries + 1)
        error_block = (
            f"YOUR PREVIOUS ANSWER WAS REJECTED. FIX THIS: {last_error}" if last_error else ""
        )
        raw = llm.generate(build_prompt(error_block), schema=schema)
        try:
            return parse(extract_json(raw))
        except Exception as exc:  # noqa: BLE001 - any failure becomes feedback for the retry
            logger.warning("%s attempt %d rejected: %s", label, attempt + 1, exc)
            last_error = str(exc)[:1500]
    raise LLMError(f"{label} failed after {max_retries + 1} attempts. Last problem: {last_error}")
