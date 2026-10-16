from typing import Literal

from pydantic import BaseModel, Field, field_validator

Kind = Literal["must", "nice"]
Status = Literal["met", "partial", "missing"]
_NULL_WORDS = {"", "null", "none", "n/a", "na", "-"}


def _blank_to_none(value):
    if isinstance(value, str) and value.strip().lower() in _NULL_WORDS:
        return None
    return value


class Tolerant(BaseModel):
    """Local models write the string "null" or "N/A" for a missing value; treat as None."""

    @field_validator("*", mode="before")
    @classmethod
    def _nulls(cls, value):
        return _blank_to_none(value)


# -- what the model returns for a posting -----------------------------------------------------


class RequirementOut(Tolerant):
    text: str
    kind: Kind
    quote: str


class PostingExtraction(Tolerant):
    title: str | None = None
    company: str | None = None
    seniority: Literal["intern", "junior", "mid", "senior", "lead", "unspecified"] = "unspecified"
    location: str | None = None
    remote: Literal["yes", "hybrid", "no", "unspecified"] = "unspecified"
    salary_text: str | None = None
    requirements: list[RequirementOut] = Field(default_factory=list, max_length=25)


# -- what we keep after verifying it against the posting text ---------------------------------


class Requirement(BaseModel):
    id: int
    text: str
    kind: Kind
    quote: str


class ParsedPosting(BaseModel):
    title: str | None = None
    company: str | None = None
    seniority: str = "unspecified"
    location: str | None = None
    remote: str = "unspecified"
    salary_text: str | None = None
    requirements: list[Requirement] = Field(default_factory=list)
    dropped: int = 0  # items the model returned that could not be found in the posting
    truncated: bool = False  # the posting was longer than the model's budget


# -- fit ---------------------------------------------------------------------------------------


class AssessmentOut(Tolerant):
    id: int
    status: Status
    cv_quote: str | None = None
    reason: str = ""


class FitExtraction(BaseModel):
    assessments: list[AssessmentOut]


class FitItem(BaseModel):
    id: int
    text: str
    kind: Kind
    status: Status  # after verification: this is the verdict that counts
    claimed_status: Status | None  # what the model said before verification (None: not assessed)
    quote: str | None  # verbatim CV evidence, only kept when it was found in the CV
    verified: bool
    reason: str = ""


class FitResult(BaseModel):
    score: int | None
    items: list[FitItem]
    downgraded: int = 0  # claims of met/partial whose evidence was not found in the CV
    cv_truncated: bool = False
