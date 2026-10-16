"""Storing CVs, postings and fit analyses; the LLM work itself lives in the parser/scorer."""

import hashlib
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from jobtrail.database.models import Application, CVVersion, FitAnalysis, Posting
from jobtrail.domain.text_match import normalize
from jobtrail.ingestion.cv import CVError, extract_cv_text
from jobtrail.llm.base import LLMClient
from jobtrail.schemas.matching import ParsedPosting
from jobtrail.services.fit_scorer import FitScorer
from jobtrail.services.posting_parser import PostingParser
from jobtrail.services.tracker import ApplicationNotFound


class NotFound(LookupError):
    pass


class CVService:
    def __init__(self, session: Session):
        self.session = session

    def add_text(self, label: str, text: str) -> tuple[CVVersion, bool]:
        """Store a CV version. Returns (version, created); the same content is stored once."""
        text = text.strip()
        if not text:
            raise CVError("The CV is empty")
        digest = hashlib.sha256(normalize(text).encode()).hexdigest()
        existing = self.session.scalar(select(CVVersion).where(CVVersion.content_hash == digest))
        if existing is not None:
            return existing, False
        version = CVVersion(label=label.strip() or "CV", content_hash=digest, text=text)
        self.session.add(version)
        self.session.flush()
        return version, True

    def add_file(self, label: str, filename: str, data: bytes) -> tuple[CVVersion, bool]:
        text = extract_cv_text(filename, data)
        default_label = filename.rsplit(".", 1)[0] if filename else "CV"
        return self.add_text(label or default_label, text)

    def list(self) -> list[CVVersion]:
        return list(self.session.scalars(select(CVVersion).order_by(CVVersion.created_at.desc())))

    def get(self, cv_id: int) -> CVVersion:
        version = self.session.get(CVVersion, cv_id)
        if version is None:
            raise NotFound(f"CV {cv_id}")
        return version

    def delete(self, version: CVVersion) -> None:
        self.session.delete(version)  # its fit analyses go with it
        self.session.flush()


def validate_url(url: str | None) -> str | None:
    """Postings link out, so only web URLs are accepted: never javascript: or file:."""
    url = (url or "").strip()
    if not url:
        return None
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("The posting URL must start with http:// or https://")
    return url


class MatchingService:
    def __init__(self, session: Session, llm: LLMClient):
        self.session = session
        self.llm = llm

    def add_posting(
        self, raw_text: str, url: str | None = None, application_id: int | None = None
    ) -> Posting:
        url = validate_url(url)
        if application_id is not None and self.session.get(Application, application_id) is None:
            raise ApplicationNotFound(application_id)
        parsed = PostingParser(self.llm).parse(raw_text)
        posting = Posting(
            application_id=application_id,
            url=url,
            raw_text=raw_text.strip(),
            parsed=parsed.model_dump(),
            model=self.llm.model,
        )
        self.session.add(posting)
        self.session.flush()
        return posting

    def analyze(self, posting: Posting, cv: CVVersion) -> FitAnalysis:
        result = FitScorer(self.llm).score(cv.text, ParsedPosting.model_validate(posting.parsed))
        analysis = FitAnalysis(
            posting=posting,
            cv_version=cv,
            score=result.score,
            detail=result.model_dump(),
            model=self.llm.model,
        )
        self.session.add(analysis)
        self.session.flush()
        return analysis

    def list_postings(self) -> list[Posting]:
        query = (
            select(Posting)
            .options(selectinload(Posting.fits), selectinload(Posting.application))
            .order_by(Posting.created_at.desc(), Posting.id.desc())
        )
        return list(self.session.scalars(query))

    def get_posting(self, posting_id: int) -> Posting:
        posting = self.session.get(Posting, posting_id)
        if posting is None:
            raise NotFound(f"Posting {posting_id}")
        return posting

    def delete_posting(self, posting: Posting) -> None:
        self.session.delete(posting)
        self.session.flush()
