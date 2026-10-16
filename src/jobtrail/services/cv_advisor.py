"""Suggest which parts of an existing CV to put forward for a posting.

The advisor never writes new CV content. It points at lines that are already in the CV, and
every pointer is verified word for word. What the CV cannot back is reported by code, not by the
model, as 'not evidenced': things not to claim."""

from jobtrail.domain.text_match import normalize, quote_in_text
from jobtrail.llm.base import LLMClient
from jobtrail.llm.retry import generate_json_with_retry
from jobtrail.schemas.matching import FitItem, ParsedPosting
from jobtrail.schemas.outreach import (
    Advice,
    AdviceExtraction,
    Highlight,
    NotEvidenced,
)
from jobtrail.services.fit_scorer import MAX_CV_CHARS
from jobtrail.services.posting_parser import truncate

MAX_HIGHLIGHTS = 6
MAX_KEYWORDS = 10

PROMPT = """You help a candidate present an EXISTING CV for a job posting. You never add \
anything that is not already in the CV.

Pick the lines of the CV that best support the requirements, most important first (at most \
{max_highlights}). Skip requirements the CV does not support. For each:
- "id": the requirement id,
- "cv_quote": the shortest exact snippet of the CV that supports it, copied character for character,
- "placement": where to put it forward: summary, experience, projects or skills,
- "why": one short sentence.
Also list up to {max_keywords} "keywords": words or phrases that appear in BOTH the requirements \
and the CV, which the candidate should keep using in the posting's own wording.

Return ONLY JSON: {{"highlights": [{{"id": 1, "cv_quote": "...", "placement": "experience", \
"why": "..."}}], "keywords": ["..."]}}

{error_block}
REQUIREMENTS:
{requirements}

CV:
<<<
{cv}
>>>"""


class CVAdvisor:
    def __init__(self, llm: LLMClient, max_retries: int = 2):
        self.llm = llm
        self.max_retries = max_retries

    def advise(
        self, cv_text: str, posting: ParsedPosting, fit_items: list[FitItem] | None = None
    ) -> Advice:
        """`fit_items` (from a fit analysis of this CV) limits the advice to requirements the
        CV was already found to support. Without it the model alone decides, which is how a
        real but irrelevant CV line ends up 'supporting' a requirement it has nothing to do
        with."""
        if not posting.requirements:
            raise ValueError("The posting has no requirements to advise on")
        cv, cv_truncated = truncate(cv_text.strip(), MAX_CV_CHARS)
        if fit_items is None:
            candidates = list(posting.requirements)
        else:
            supported = {i.id for i in fit_items if i.status in ("met", "partial")}
            candidates = [r for r in posting.requirements if r.id in supported]
        if not candidates:
            return Advice(
                highlights=[],
                keywords=[],
                not_evidenced=[
                    NotEvidenced(id=r.id, text=r.text, kind=r.kind) for r in posting.requirements
                ],
                cv_truncated=cv_truncated,
            )
        by_id = {r.id: r for r in candidates}
        listing = "\n".join(
            f'{r.id}. [{r.kind}] {r.text} (posting: "{r.quote}")' for r in candidates
        )
        posting_terms = "\n".join(f"{r.text}\n{r.quote}" for r in posting.requirements)
        attempt = 0

        def build(error_block: str) -> str:
            return PROMPT.format(
                max_highlights=MAX_HIGHLIGHTS,
                max_keywords=MAX_KEYWORDS,
                error_block=error_block,
                requirements=listing,
                cv=cv,
            )

        def parse(data: dict) -> Advice:
            nonlocal attempt
            attempt += 1
            extraction = AdviceExtraction.model_validate(data)

            highlights: list[Highlight] = []
            seen: set[int] = set()
            rejected: list[str] = []
            for item in extraction.highlights:
                requirement = by_id.get(item.id)
                if requirement is None or item.id in seen:
                    continue
                if not quote_in_text(item.cv_quote, cv):
                    rejected.append(f"{item.id}: {item.cv_quote!r}")
                    continue
                seen.add(item.id)
                highlights.append(
                    Highlight(
                        id=item.id,
                        requirement=requirement.text,
                        kind=requirement.kind,
                        cv_quote=item.cv_quote,
                        placement=item.placement,
                        why=item.why,
                    )
                )
            if rejected and attempt <= self.max_retries:
                raise ValueError(
                    "These cv_quotes are not in the CV; copy exact CV words or skip the "
                    "requirement: " + "; ".join(rejected)
                )

            keywords: list[str] = []
            for word in extraction.keywords:
                if (
                    normalize(word) not in {normalize(k) for k in keywords}
                    and quote_in_text(word, cv)
                    and quote_in_text(word, posting_terms)
                ):
                    keywords.append(word)

            not_evidenced = [
                NotEvidenced(id=r.id, text=r.text, kind=r.kind)
                for r in posting.requirements
                if r.id not in seen
            ]
            return Advice(
                highlights=highlights[:MAX_HIGHLIGHTS],
                keywords=keywords[:MAX_KEYWORDS],
                not_evidenced=not_evidenced,
                dropped=len(rejected),
                cv_truncated=cv_truncated,
            )

        return generate_json_with_retry(
            self.llm,
            label="CV advisor",
            max_retries=self.max_retries,
            build_prompt=build,
            parse=parse,
            schema=AdviceExtraction.model_json_schema(),
        )
