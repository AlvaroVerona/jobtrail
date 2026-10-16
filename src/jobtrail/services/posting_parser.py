"""Extract structured requirements from a job posting, keeping only what the posting says."""

import re

from jobtrail.domain.text_match import clean_quote, normalize, quote_in_text
from jobtrail.llm.base import LLMClient
from jobtrail.llm.retry import generate_json_with_retry
from jobtrail.schemas.matching import ParsedPosting, PostingExtraction, Requirement

MAX_POSTING_CHARS = 8000
MAX_REQUIREMENTS = 15

PROMPT = """You extract structured data from a job posting. Copy words from the posting; \
never invent anything.

Return ONLY a JSON object with these fields:
- "title", "company", "location", "salary_text": copy the words exactly as they appear in the \
posting, or null if the posting does not say.
- "seniority": one of intern, junior, mid, senior, lead, unspecified.
- "remote": one of yes, hybrid, no, unspecified.
- "requirements": the concrete skills, tools, experience and qualifications the posting asks \
for (not benefits, perks or company description). At most {max_requirements}. Each item has:
    "text": a label of at most 12 words,
    "kind": "must" if the posting requires it, "nice" if it is a plus or desirable,
    "quote": the exact words that state it, copied character for character from ONE line or \
sentence of the posting. Do not include section headings such as "Nice to have:" or bullet \
markers, and do not join text from different lines.

{error_block}
JOB POSTING:
<<<
{posting}
>>>"""


def truncate(text: str, limit: int) -> tuple[str, bool]:
    return (text[:limit], True) if len(text) > limit else (text, False)


class PostingParser:
    def __init__(self, llm: LLMClient, max_retries: int = 2):
        self.llm = llm
        self.max_retries = max_retries

    def parse(self, raw_text: str) -> ParsedPosting:
        text, truncated = truncate(raw_text.strip(), MAX_POSTING_CHARS)
        if not text:
            raise ValueError("The posting text is empty")

        def build(error_block: str) -> str:
            return PROMPT.format(
                max_requirements=MAX_REQUIREMENTS, error_block=error_block, posting=text
            )

        parsed = generate_json_with_retry(
            self.llm,
            label="posting parser",
            max_retries=self.max_retries,
            build_prompt=build,
            parse=lambda data: self._verify(PostingExtraction.model_validate(data), text),
            schema=PostingExtraction.model_json_schema(),
        )
        parsed.truncated = truncated
        return parsed

    @staticmethod
    def _verify(extraction: PostingExtraction, text: str) -> ParsedPosting:
        requirements: list[Requirement] = []
        seen: set[str] = set()
        dropped = 0
        missing_quotes: list[str] = []
        for item in extraction.requirements[:MAX_REQUIREMENTS]:
            key = normalize(item.text)
            if not quote_in_text(item.quote, text):
                dropped += 1
                missing_quotes.append(item.quote)
                continue
            if key in seen:
                continue
            seen.add(key)
            quote = re.sub(r"^[\s\-\u2022*\u00b7\u2013\u2014]+", "", item.quote)  # bullet markers
            requirements.append(
                Requirement(id=len(requirements) + 1, text=item.text, kind=item.kind, quote=quote)
            )

        if not requirements:
            sample = "; ".join(repr(q) for q in missing_quotes[:3]) or "none were returned"
            raise ValueError(
                "No requirement quote was found verbatim in the posting. Copy the quote "
                f"exactly as written. Quotes not found: {sample}"
            )

        def verified(value: str | None) -> str | None:
            return value if value and clean_quote(value) and quote_in_text(value, text) else None

        return ParsedPosting(
            title=verified(extraction.title),
            company=verified(extraction.company),
            seniority=extraction.seniority,
            location=verified(extraction.location),
            remote=extraction.remote,
            salary_text=verified(extraction.salary_text),
            requirements=requirements,
            dropped=dropped,
        )
