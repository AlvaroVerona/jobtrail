"""Synthetic demo data, so the project can be tried without any personal information."""

import csv
from datetime import UTC, date, datetime, time, timedelta
from importlib.resources import files
from io import StringIO

from sqlalchemy.orm import Session

from jobtrail.domain.status import Status
from jobtrail.ingestion.csv_import import ImportReport, import_csv
from jobtrail.services.contacts import ContactService, ReminderService
from jobtrail.services.tracker import TrackerService

DEMO_DB_FILE = "demo.db"


def _shift_to_recent(text: str, today: date) -> str:
    """Move every date so the latest application was sent three days ago.

    The demo is a snapshot with fixed dates; left alone it would go stale, leaving the weekly
    chart empty and nothing overdue on the follow-ups page. The gaps between dates are kept,
    except that no reply is dated in the future: a reply that would land after yesterday is
    pulled back to yesterday."""
    reader = csv.DictReader(StringIO(text))
    rows = list(reader)
    applied = [date.fromisoformat(r["Applied date"]) for r in rows if r["Applied date"]]
    if not applied:
        return text
    delta = (today - timedelta(days=3)) - max(applied)
    yesterday = today - timedelta(days=1)
    for row in rows:
        if row["Applied date"]:
            row["Applied date"] = (date.fromisoformat(row["Applied date"]) + delta).isoformat()
        if row["Response date"]:
            shifted = date.fromisoformat(row["Response date"]) + delta
            row["Response date"] = min(shifted, yesterday).isoformat()
    out = StringIO()
    writer = csv.DictWriter(out, fieldnames=reader.fieldnames, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue()


# Invented names, cycled through. Any resemblance to a real person is chance.
_NAMES = [
    ("Sam Rivera", "Talent Acquisition"),
    ("Alex Quinn", "Recruiter"),
    ("Jordan Blake", "HR Manager"),
    ("Taylor Morgan", "People & Culture"),
    ("Riley Chen", "Recruiter"),
    ("Casey Ortiz", "Hiring Manager"),
    ("Morgan Ellis", "Talent Acquisition"),
    ("Jamie Novak", "HR Manager"),
    ("Robin Hale", "Recruiter"),
    ("Drew Santos", "People & Culture"),
    ("Avery Kim", "Hiring Manager"),
    ("Quinn Foster", "Talent Acquisition"),
    ("Sasha Lind", "Recruiter"),
    ("Devon Park", "HR Manager"),
]


def _add_demo_people(session: Session, today: date) -> None:
    """Contacts, messages and reminders around the demo applications, so the CRM is not empty.

    Referral applications come with a contact who answered; a few old, unanswered applications
    come with a contact you are still waiting on. The dates are relative to `today`."""
    tracker = TrackerService(session)
    contacts = ContactService(session)
    reminders = ReminderService(session)
    applications = sorted(tracker.list_applications(), key=lambda a: a.id)
    noon = lambda d: datetime.combine(d, time(12, 0), tzinfo=UTC)  # noqa: E731
    names = iter(_NAMES * 2)

    def person(application, source):
        name, title = next(names)
        return contacts.create(application.company.name, name, job_title=title, source=source)

    for application in applications:
        channel = application.channel.name if application.channel else ""
        if channel == "Referral" and application.applied_at:
            contact = person(application, "referral")
            sent = application.applied_at - timedelta(days=2)
            contacts.add_interaction(
                contact,
                "message",
                direction="outbound",
                channel="linkedin",
                summary="Asked for an introduction",
                at=sent,
                application=application,
            )
            contacts.add_interaction(
                contact,
                "message",
                direction="inbound",
                channel="linkedin",
                summary="Happy to help, passing your CV on",
                at=sent + timedelta(days=1),
                application=application,
            )

    recruiters = [a for a in applications if a.channel and a.channel.name == "Recruiter"]
    for index, application in enumerate(recruiters[:4]):
        contact = person(application, "search")
        sent = noon(today - timedelta(days=14 + index))
        contacts.add_interaction(
            contact,
            "message",
            direction="outbound",
            channel="email",
            summary="Introduced myself and the role",
            at=sent,
            application=application,
        )
        if index % 2 == 0:
            contacts.add_interaction(
                contact,
                "message",
                direction="inbound",
                channel="email",
                summary="Replied with next steps",
                at=sent + timedelta(days=2),
                application=application,
            )

    waiting = [
        a
        for a in applications
        if a.status in (Status.APPLIED, Status.NO_RESPONSE)
        and a.applied_at
        and a.company.contacts == []
    ]
    waiting.sort(key=lambda a: a.applied_at)
    for index, application in enumerate(waiting[:4]):
        contact = person(application, "posting" if index % 2 else "search")
        contacts.add_interaction(
            contact,
            "message",
            direction="outbound",
            channel="linkedin" if index % 2 else "email",
            summary="Followed up on my application",
            at=noon(today - timedelta(days=6 + index * 4)),
            application=application,
        )
        reminders.add(
            due_on=today + timedelta(days=(-1, 0, 3, 7)[index]),
            contact=contact,
            note=f"Check in with {contact.name}" if index != 1 else None,
        )


def load_demo(session: Session, today: date | None = None) -> ImportReport:
    today = today or date.today()
    text = (files("jobtrail") / "demo_data" / "demo_applications.csv").read_text(encoding="utf-8")
    report = import_csv(session, StringIO(_shift_to_recent(text, today)))
    _add_demo_people(session, today)
    session.commit()
    return report
