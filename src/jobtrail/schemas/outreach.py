from typing import Literal

from pydantic import BaseModel, Field

from jobtrail.schemas.matching import Kind, Tolerant

Placement = Literal["summary", "experience", "projects", "skills"]


# -- message drafts ----------------------------------------------------------------------------


class DraftExtraction(Tolerant):
    subject: str | None = None
    body: str
    cv_quotes: list[str] = Field(default_factory=list)


class Draft(BaseModel):
    kind: str
    language: str
    subject: str | None
    body: str
    cv_quotes: list[str]  # the CV words the message relies on, each found in the CV
    warnings: list[str] = Field(default_factory=list)


# -- CV advice ---------------------------------------------------------------------------------


class HighlightOut(Tolerant):
    id: int
    cv_quote: str
    placement: Placement = "experience"
    why: str = ""


class AdviceExtraction(Tolerant):
    highlights: list[HighlightOut] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)


class Highlight(BaseModel):
    id: int
    requirement: str
    kind: Kind
    cv_quote: str
    placement: Placement
    why: str = ""


class NotEvidenced(BaseModel):
    id: int
    text: str
    kind: Kind


class Advice(BaseModel):
    highlights: list[Highlight]
    keywords: list[str]  # terms present in both the posting and the CV
    not_evidenced: list[NotEvidenced]  # requirements with no verified evidence: do not claim
    dropped: int = 0
    cv_truncated: bool = False
