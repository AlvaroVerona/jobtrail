"""Draft a short outreach message that cannot claim anything the CV does not say.

Two deterministic checks run on every draft: the CV words the message relies on must be in the
CV, and every number it contains must come from the CV or the facts it was given. A draft that
fails is sent back to the model; on the last attempt it is kept but carries visible warnings,
because it is only a draft the user edits and sends themselves."""

from datetime import datetime

from jobtrail.database.models import Application, utcnow
from jobtrail.domain.followup import FollowupKind
from jobtrail.domain.text_match import quote_in_text, unsupported_numbers, unsupported_terms
from jobtrail.llm.base import LLMClient
from jobtrail.llm.retry import generate_json_with_retry
from jobtrail.schemas.outreach import Draft, DraftExtraction
from jobtrail.services.analytics import last_activity
from jobtrail.services.fit_scorer import MAX_CV_CHARS
from jobtrail.services.posting_parser import truncate

LANGUAGES = {"en": "English", "es": "Spanish"}
MAX_WORDS = 180

GUIDANCE = {
    FollowupKind.FOLLOW_UP: (
        "a polite follow-up on a job application: ask for an update, restate interest in "
        "one sentence, and mention how long ago the application was sent."
    ),
    FollowupKind.THANK_YOU: (
        "a thank-you note after a conversation about the role: thank them, reaffirm interest, "
        "and do not mention anything specific that was said, because you were not told."
    ),
    FollowupKind.RECRUITER_INTRO: (
        "a first message to a person at the company: introduce the candidate in one sentence, "
        "say why the role is a fit, and ask for a short chat."
    ),
}

PROMPT = """You write {guidance}
Write in {language}. Plain text, 60 to 120 words, no markdown.

FACTS (the only things you may state):
{facts}
{cv_block}
Rules:
- Do not invent employers, tools, results, numbers or dates. Do not use any number that is not \
in the facts or the CV.
- Greeting: use the recipient's name if given, otherwise a neutral greeting. End with a sign-off \
line but no name.
- "subject": a short email subject, or null if it is not an email.
- "cv_quotes": the exact CV words any claim about the candidate relies on, copied character for \
character. If you say nothing about the candidate's experience, use an empty list.

Return ONLY JSON: {{"subject": "...", "body": "...", "cv_quotes": ["..."]}}
{error_block}"""


def build_facts(application: Application, recipient: str | None, now: datetime) -> str:
    posting = application.postings[-1].parsed if application.postings else {}
    role = application.role or posting.get("title") or "the role"
    lines = [
        f"Company: {application.company.name}",
        f"Role: {role}",
        f"Application status: {application.status.value.replace('_', ' ')}",
    ]
    if application.applied_at:
        days = (now - application.applied_at).days
        lines.append(f"Applied on {application.applied_at:%d %B %Y}, {days} days ago")
    lines.append(f"Messages already sent about this application: {len(application.followups)}")
    if application.followups:
        lines.append(f"Days since the last activity: {(now - last_activity(application)).days}")
    if recipient:
        lines.append(f"Recipient: {recipient}")
    if posting.get("location"):
        lines.append(f"Location: {posting['location']}")
    # The posting's requirements are deliberately not given to the model: it tends to present
    # them as the candidate's own experience.
    return "\n".join(lines)


class MessageDrafter:
    def __init__(self, llm: LLMClient, max_retries: int = 2):
        self.llm = llm
        self.max_retries = max_retries

    def draft(
        self,
        application: Application,
        kind: FollowupKind,
        *,
        language: str = "en",
        recipient: str | None = None,
        cv_text: str | None = None,
        now: datetime | None = None,
    ) -> Draft:
        if language not in LANGUAGES:
            raise ValueError(f"Language must be one of: {', '.join(LANGUAGES)}")
        kind = FollowupKind(kind)
        now = now or utcnow()
        facts = build_facts(application, recipient.strip() or None if recipient else None, now)
        cv, _ = truncate(cv_text.strip(), MAX_CV_CHARS) if cv_text else ("", False)
        cv_block = (
            f"\nCANDIDATE CV (you may rely on it):\n<<<\n{cv}\n>>>\n"
            if cv
            else ("\nNo CV was given, so say nothing about the candidate's experience.\n")
        )
        allowed_text = f"{facts}\n{cv}"
        attempt = 0

        def build(error_block: str) -> str:
            return PROMPT.format(
                guidance=GUIDANCE[kind],
                language=LANGUAGES[language],
                facts=facts,
                cv_block=cv_block,
                error_block=error_block,
            )

        def parse(data: dict) -> Draft:
            nonlocal attempt
            attempt += 1
            extraction = DraftExtraction.model_validate(data)
            body = extraction.body.strip()
            if not body:
                raise ValueError("The body is empty")
            if len(body.split()) > MAX_WORDS:
                raise ValueError(f"The body is longer than {MAX_WORDS} words; make it shorter")

            problems: list[str] = []
            quotes = [q for q in extraction.cv_quotes if q and q.strip()]
            bad_quotes = [q for q in quotes if not (cv and quote_in_text(q, cv))]
            if bad_quotes:
                problems.append(
                    "these cv_quotes are not in the CV (copy exact CV words, or leave the list "
                    "empty and remove the claim): " + "; ".join(repr(q) for q in bad_quotes)
                )
            text = f"{extraction.subject or ''}\n{body}"
            bad_numbers = unsupported_numbers(text, allowed_text)
            bad_terms = unsupported_terms(text, allowed_text)
            if bad_numbers:
                problems.append(
                    "these numbers are not in the facts or the CV, remove them: "
                    + ", ".join(bad_numbers)
                )
            if bad_terms:
                problems.append(
                    "these names are not in the facts or the CV, remove them: "
                    + ", ".join(bad_terms)
                )
            if problems and attempt <= self.max_retries:
                raise ValueError("; ".join(problems))

            warnings = []
            if bad_quotes:
                warnings.append(
                    "Some claims about you could not be matched to your CV. Check every "
                    "statement about your experience before sending."
                )
            if bad_numbers:
                warnings.append(
                    "The draft contains numbers that are not in your CV or the application: "
                    + ", ".join(bad_numbers)
                    + ". Remove or correct them."
                )
            if bad_terms:
                warnings.append(
                    "The draft names things that are not in your CV or the application: "
                    + ", ".join(bad_terms)
                    + ". Remove or correct them."
                )
            return Draft(
                kind=kind.value,
                language=language,
                subject=(extraction.subject or "").strip() or None,
                body=body,
                cv_quotes=[q for q in quotes if q not in bad_quotes],
                warnings=warnings,
            )

        return generate_json_with_retry(
            self.llm,
            label="message drafter",
            max_retries=self.max_retries,
            build_prompt=build,
            parse=parse,
            schema=DraftExtraction.model_json_schema(),
        )
