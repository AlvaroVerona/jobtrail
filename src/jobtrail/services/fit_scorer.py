"""Compare a CV to a posting's requirements, trusting only evidence found in the CV.

The model proposes a verdict and a quote for each requirement. Code then checks that the
quote is really in the CV; a claim without that evidence is downgraded to "missing". The
score is arithmetic over the surviving verdicts. So the model can be wrong about what a
quote means, but it cannot invent experience the CV does not contain.
"""

from jobtrail.domain.scoring import fit_score
from jobtrail.domain.text_match import quote_in_text
from jobtrail.llm.base import LLMClient
from jobtrail.llm.retry import generate_json_with_retry
from jobtrail.schemas.matching import (
    AssessmentOut,
    FitExtraction,
    FitItem,
    FitResult,
    ParsedPosting,
)
from jobtrail.services.posting_parser import truncate

MAX_CV_CHARS = 8000

PROMPT = """You compare a candidate's CV with the requirements of a job posting.

For EACH requirement decide:
- "met": the CV clearly shows it.
- "partial": the CV shows something related but not the whole requirement.
- "missing": the CV does not show it.

For "met" and "partial" you MUST give "cv_quote": the shortest exact snippet of the CV text \
below that shows it (a skill name or one line), copied character for character from the CV. \
The quote must come from the CV, never from the requirements list: repeating the requirement \
is not evidence. If the CV has no words that show it, the answer is "missing" and "cv_quote" \
is null. Never infer skills the CV does not state. Add a "reason" of one short sentence.

Return ONLY a JSON object: {{"assessments": [{{"id": 1, "status": "met", "cv_quote": "...", \
"reason": "..."}}]}} with one entry per requirement, using the requirement ids.

{error_block}
REQUIREMENTS:
{requirements}

CV:
<<<
{cv}
>>>"""


class FitScorer:
    def __init__(self, llm: LLMClient, max_retries: int = 2):
        self.llm = llm
        self.max_retries = max_retries

    def score(self, cv_text: str, posting: ParsedPosting) -> FitResult:
        if not posting.requirements:
            raise ValueError("The posting has no requirements to compare")
        cv, cv_truncated = truncate(cv_text.strip(), MAX_CV_CHARS)
        listing = "\n".join(f"{r.id}. [{r.kind}] {r.text}" for r in posting.requirements)
        attempt = 0

        def build(error_block: str) -> str:
            return PROMPT.format(error_block=error_block, requirements=listing, cv=cv)

        def parse(data: dict) -> FitResult:
            nonlocal attempt
            attempt += 1
            result = self._verify(FitExtraction.model_validate(data), posting, cv)
            unverified = [
                f"{i.id}: {i.claimed_status!r} with quote not found in the CV"
                for i in result.items
                if i.claimed_status in ("met", "partial") and not i.verified
            ]
            if unverified and attempt <= self.max_retries:
                raise ValueError(
                    "Some quotes are not in the CV. Copy the CV's exact words, or answer "
                    "'missing' with a null cv_quote. " + "; ".join(unverified)
                )
            return result

        result = generate_json_with_retry(
            self.llm,
            label="fit scorer",
            max_retries=self.max_retries,
            build_prompt=build,
            parse=parse,
            schema=FitExtraction.model_json_schema(),
        )
        result.cv_truncated = cv_truncated
        return result

    @staticmethod
    def _verify(extraction: FitExtraction, posting: ParsedPosting, cv: str) -> FitResult:
        by_id: dict[int, AssessmentOut] = {}
        for assessment in extraction.assessments:
            by_id.setdefault(assessment.id, assessment)

        items: list[FitItem] = []
        downgraded = 0
        for requirement in posting.requirements:
            claim = by_id.get(requirement.id)
            if claim is None:
                items.append(
                    FitItem(
                        id=requirement.id,
                        text=requirement.text,
                        kind=requirement.kind,
                        status="missing",
                        claimed_status=None,
                        quote=None,
                        verified=False,
                        reason="The model did not assess this requirement.",
                    )
                )
                continue

            claims_evidence = claim.status in ("met", "partial")
            found = claims_evidence and quote_in_text(claim.cv_quote, cv)
            if claims_evidence and not found:
                downgraded += 1
            items.append(
                FitItem(
                    id=requirement.id,
                    text=requirement.text,
                    kind=requirement.kind,
                    status=claim.status if found or not claims_evidence else "missing",
                    claimed_status=claim.status,
                    quote=claim.cv_quote if found else None,
                    verified=found or not claims_evidence,
                    reason=claim.reason,
                )
            )

        score = fit_score((i.kind, i.status) for i in items)
        return FitResult(score=score, items=items, downgraded=downgraded)
