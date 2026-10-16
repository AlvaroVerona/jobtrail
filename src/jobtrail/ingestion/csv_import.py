"""Import applications from a CSV file.

Headers are matched case-insensitively against a list of aliases, so the same importer reads
a hand-made spreadsheet or a Notion database export (English or Spanish column names).
Rows are independent: a bad row is reported and skipped, it never aborts the import.
Importing the same file twice is safe; rows already present are skipped.
"""

import csv
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TextIO

from sqlalchemy import select
from sqlalchemy.orm import Session

from jobtrail.database.models import Application
from jobtrail.domain.status import SUBMITTED_STATUSES, Status, can_transition
from jobtrail.services.tracker import TrackerService

COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "company": ("company", "empresa"),
    "role": ("role", "rol", "puesto", "position", "title"),
    "status": ("status", "estado"),
    "applied_at": ("applied date", "applied_at", "applied", "fecha", "fecha aplicacion"),
    "responded_at": ("fecha respuesta", "response date", "responded_at"),
    "channel": ("canal", "channel"),
    "track": ("track",),
    "salary_expected": ("expected salary", "salary_expected"),
    "salary_asked": ("asked salary", "salary_asked"),
    "notes": ("notes", "notas"),
}

STATUS_ALIASES: dict[str, Status] = {
    "wishlist": Status.WISHLIST,
    "to apply": Status.TO_APPLY,
    "applied": Status.APPLIED,
    "follow up sent": Status.FOLLOW_UP_SENT,
    "follow up enviado": Status.FOLLOW_UP_SENT,
    "screening": Status.SCREENING,
    "interviewing": Status.INTERVIEWING,
    "interview": Status.INTERVIEWING,
    "offer": Status.OFFER,
    "rejected": Status.REJECTED,
    "no response": Status.NO_RESPONSE,
    "sin respuesta": Status.NO_RESPONSE,
    "withdrawn": Status.WITHDRAWN,
}

# Spanish/legacy channel names map onto the seeded English ones.
CHANNEL_ALIASES: dict[str, str] = {
    "web de empresa": "Company website",
    "referido/contacto": "Referral",
    "referido": "Referral",
    "email directo": "Direct email",
}

_DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%B %d, %Y", "%b %d, %Y", "%d-%m-%Y")


class RowError(ValueError):
    pass


@dataclass
class ImportReport:
    created: int = 0
    skipped_duplicates: int = 0
    errors: list[tuple[int, str]] = field(default_factory=list)  # (csv line number, message)

    @property
    def total_rows(self) -> int:
        return self.created + self.skipped_duplicates + len(self.errors)


def _normalise(text: str) -> str:
    return re.sub(r"[\s_\-]+", " ", text.strip().lower())


def parse_status(value: str) -> Status:
    key = _normalise(value)
    try:
        return STATUS_ALIASES[key]
    except KeyError:
        raise RowError(f"Unknown status '{value}'") from None


def parse_date(value: str) -> datetime:
    """Dates without a time are taken as midnight UTC. Day-first is assumed for 12/05/2026."""
    value = value.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    raise RowError(f"Unrecognised date '{value}'")


def parse_salary(value: str) -> int:
    cleaned = re.sub(r"[^\d.,]", "", value)
    if not cleaned:
        raise RowError(f"Unrecognised salary '{value}'")
    if re.fullmatch(r"\d{1,3}([.,]\d{3})+", cleaned):  # 45.000 / 45,000 -> thousands separator
        cleaned = re.sub(r"[.,]", "", cleaned)
    else:
        cleaned = cleaned.replace(",", ".")
    try:
        return round(float(cleaned))
    except ValueError:
        raise RowError(f"Unrecognised salary '{value}'") from None


def _resolve_columns(headers: list[str]) -> dict[str, str]:
    by_normalised = {_normalise(h): h for h in headers if h}
    resolved: dict[str, str] = {}
    for field_name, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            if _normalise(alias) in by_normalised:
                resolved[field_name] = by_normalised[_normalise(alias)]
                break
    return resolved


def _existing_keys(session: Session) -> set[tuple[str, str, object]]:
    keys = set()
    for app in session.scalars(select(Application)):
        keys.add(_key(app.company.name, app.role, app.applied_at))
    return keys


def _key(company: str, role: str | None, applied_at: datetime | None):
    return (company.strip().lower(), (role or "").strip().lower(), applied_at and applied_at.date())


def import_csv(session: Session, source: TextIO, *, dry_run: bool = False) -> ImportReport:
    """Import rows from an open text file. With dry_run nothing is written."""
    reader = csv.DictReader(source)
    columns = _resolve_columns(reader.fieldnames or [])
    if "company" not in columns:
        raise ValueError("CSV needs a company column (one of: company, empresa)")

    tracker = TrackerService(session)
    seen = _existing_keys(session)
    report = ImportReport()

    for row in reader:
        line = reader.line_num

        def cell(name: str, row=row) -> str:
            column = columns.get(name)
            return (row.get(column) or "").strip() if column else ""

        try:
            parsed = _parse_row(cell)
        except RowError as exc:
            report.errors.append((line, str(exc)))
            continue

        key = _key(parsed["company"], parsed["role"], parsed["applied_at"])
        if key in seen:
            report.skipped_duplicates += 1
            continue
        seen.add(key)
        if not dry_run:
            _create(tracker, parsed)
        report.created += 1

    if dry_run:
        session.rollback()
    else:
        session.commit()
    return report


def _parse_row(cell) -> dict:
    company = cell("company")
    if not company:
        raise RowError("Missing company")

    status = parse_status(cell("status")) if cell("status") else Status.WISHLIST
    applied_at = parse_date(cell("applied_at")) if cell("applied_at") else None
    responded_at = parse_date(cell("responded_at")) if cell("responded_at") else None
    if status in SUBMITTED_STATUSES and applied_at is None:
        raise RowError(f"Applied date is required for status '{status.value}'")

    channel = cell("channel")
    return {
        "company": company,
        "role": cell("role") or None,
        "status": status,
        "applied_at": applied_at,
        "responded_at": responded_at,
        "channel": CHANNEL_ALIASES.get(_normalise(channel), channel) or None,
        "track": cell("track") or None,
        "salary_expected": parse_salary(cell("salary_expected"))
        if cell("salary_expected")
        else None,
        "salary_asked": parse_salary(cell("salary_asked")) if cell("salary_asked") else None,
        "notes": cell("notes") or None,
    }


def _create(tracker: TrackerService, parsed: dict) -> None:
    """Rebuild the history as faithfully as the data allows.

    A submitted application starts at 'applied' on its applied date. If the row also has a
    response date and the move is legal, the final status is recorded on that date. Otherwise
    the final status is recorded directly, at the applied date, because the real date of the
    change is unknown."""
    status: Status = parsed["status"]
    applied_at: datetime | None = parsed["applied_at"]
    responded_at: datetime | None = parsed["responded_at"]
    details = {
        key: parsed[key]
        for key in ("channel", "track", "salary_expected", "salary_asked", "notes", "role")
    }

    two_step = (
        status in SUBMITTED_STATUSES
        and status is not Status.APPLIED
        and responded_at is not None
        and applied_at is not None
        and responded_at >= applied_at
        and can_transition(Status.APPLIED, status)
    )
    if two_step:
        application = tracker.create_application(
            parsed["company"],
            status=Status.APPLIED,
            applied_at=applied_at,
            at=applied_at,
            **details,
        )
        tracker.change_status(application, status, at=responded_at, note="Imported")
    else:
        tracker.create_application(
            parsed["company"],
            status=status,
            applied_at=applied_at,
            at=applied_at,
            **details,
        )
