"""People a job posting itself names as the contact: a recruiter, a hiring manager, an HR person.

Only what the text says. Each suggestion must come with a quote that is found in the posting and
that contains the person's name, so the model cannot supply a name from its own memory. Nothing
is saved: you look at the suggestions and add the ones you want."""

from pydantic import BaseModel, Field

from jobtrail.domain.text_match import quote_in_text
from jobtrail.llm.base import LLMClient
from jobtrail.llm.retry import generate_json_with_retry
from jobtrail.schemas.matching import Tolerant
from jobtrail.services.posting_parser import MAX_POSTING_CHARS, truncate

MAX_PEOPLE = 5

PROMPT = """Find the people that this job posting itself names as a point of contact: a \
recruiter, a hiring manager, someone in HR or talent acquisition to write to. Only people whose \
name is written in the posting. If the posting names nobody, return an empty list. Never guess a \
name and never use a name from your own knowledge.

For each person give:
- "name": the person's name exactly as written in the posting,
- "title": their role if the posting states it, otherwise null,
- "quote": the exact words of the posting that mention them, copied character for character, \
and containing the name.

Return ONLY JSON: {{"people": [{{"name": "...", "title": "...", "quote": "..."}}]}}

{error_block}
JOB POSTING:
<<<
{posting}
>>>"""


class MentionOut(Tolerant):
    name: str
    title: str | None = None
    quote: str


class ExtractionOut(Tolerant):
    people: list[MentionOut] = Field(default_factory=list)


class PersonMention(BaseModel):
    name: str
    title: str | None
    quote: str


class ContactExtractor:
    def __init__(self, llm: LLMClient, max_retries: int = 2):
        self.llm = llm
        self.max_retries = max_retries

    def find_people(self, posting_text: str) -> list[PersonMention]:
        text, _ = truncate(posting_text.strip(), MAX_POSTING_CHARS)
        if not text:
            raise ValueError("The posting text is empty")
        attempt = 0

        def build(error_block: str) -> str:
            return PROMPT.format(error_block=error_block, posting=text)

        def parse(data: dict) -> list[PersonMention]:
            nonlocal attempt
            attempt += 1
            extraction = ExtractionOut.model_validate(data)
            people: list[PersonMention] = []
            rejected: list[str] = []
            for item in extraction.people:
                verified = (
                    quote_in_text(item.quote, text)
                    and quote_in_text(item.name, item.quote)
                    and quote_in_text(item.name, text)
                )
                if not verified:
                    rejected.append(f"{item.name!r}")
                elif all(p.name.casefold() != item.name.casefold() for p in people):
                    people.append(PersonMention(name=item.name, title=item.title, quote=item.quote))
            if rejected and attempt <= self.max_retries:
                raise ValueError(
                    "These people are not in the posting, or the quote does not contain the name. "
                    "Return only names written in the posting, each with its exact quote: "
                    + ", ".join(rejected)
                )
            return people[:MAX_PEOPLE]

        return generate_json_with_retry(
            self.llm,
            label="contact extractor",
            max_retries=self.max_retries,
            build_prompt=build,
            parse=parse,
            schema=ExtractionOut.model_json_schema(),
        )
