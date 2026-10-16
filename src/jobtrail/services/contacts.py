"""The people side of the search: contacts, what was said, whether they answered, reminders."""

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from urllib.parse import quote_plus

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from jobtrail.database.models import (
    Application,
    Contact,
    Followup,
    Interaction,
    Reminder,
    utcnow,
)
from jobtrail.domain.outreach import (
    Channel,
    Direction,
    Kind,
    Source,
    ThreadState,
    check_interaction,
    thread_state,
)
from jobtrail.domain.timeutil import require_aware
from jobtrail.services.matching import validate_url
from jobtrail.services.tracker import TrackerService

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_EDITABLE = frozenset({"name", "job_title", "profile_url", "email", "source", "notes"})


class ContactNotFound(LookupError):
    pass


class ReminderNotFound(LookupError):
    pass


def _clean(value: str | None) -> str | None:
    value = (value or "").strip()
    return value or None


def _check_email(email: str | None) -> str | None:
    email = _clean(email)
    if email is not None and not _EMAIL.match(email):
        raise ValueError(f"'{email}' does not look like an email address")
    return email.lower() if email else None


@dataclass(frozen=True)
class ContactRow:
    contact: Contact
    state: ThreadState


class ContactService:
    def __init__(self, session: Session):
        self.session = session
        self.tracker = TrackerService(session)

    # -- contacts ------------------------------------------------------------------------

    def create(
        self,
        company: str,
        name: str,
        *,
        job_title: str | None = None,
        profile_url: str | None = None,
        email: str | None = None,
        source: str = "other",
        notes: str | None = None,
    ) -> Contact:
        name = (name or "").strip()
        if not name:
            raise ValueError("A contact needs a name")
        company_row = self.tracker.get_or_create_company(company)
        clash = self.session.scalar(
            select(Contact).where(
                Contact.company_id == company_row.id, func.lower(Contact.name) == name.lower()
            )
        )
        if clash is not None:
            raise ValueError(f"{name} is already a contact at {company_row.name}")
        contact = Contact(
            company=company_row,
            name=name,
            job_title=_clean(job_title),
            profile_url=validate_url(profile_url),
            email=_check_email(email),
            source=Source(source).value,
            notes=_clean(notes),
        )
        self.session.add(contact)
        self.session.flush()
        return contact

    def get(self, contact_id: int) -> Contact:
        contact = self.session.get(Contact, contact_id)
        if contact is None:
            raise ContactNotFound(contact_id)
        return contact

    def update(self, contact: Contact, **fields) -> Contact:
        unknown = set(fields) - _EDITABLE
        if unknown:
            raise ValueError(f"Cannot edit: {', '.join(sorted(unknown))}")
        if "name" in fields:
            name = (fields["name"] or "").strip()
            if not name:
                raise ValueError("A contact needs a name")
            clash = self.session.scalar(
                select(Contact).where(
                    Contact.company_id == contact.company_id,
                    func.lower(Contact.name) == name.lower(),
                    Contact.id != contact.id,
                )
            )
            if clash is not None:
                raise ValueError(f"{name} is already a contact at {contact.company.name}")
            fields["name"] = name
        if "profile_url" in fields:
            fields["profile_url"] = validate_url(fields["profile_url"])
        if "email" in fields:
            fields["email"] = _check_email(fields["email"])
        if "source" in fields:
            fields["source"] = Source(fields["source"]).value
        for key in ("job_title", "notes"):
            if key in fields:
                fields[key] = _clean(fields[key])
        for key, value in fields.items():
            setattr(contact, key, value)
        self.session.flush()
        return contact

    def delete(self, contact: Contact) -> None:
        # Messages that were also logged on an application stay there; only the person goes.
        self.session.delete(contact)
        self.session.flush()

    def _query(self):
        return select(Contact).options(
            selectinload(Contact.company), selectinload(Contact.interactions)
        )

    def list_contacts(
        self,
        *,
        search: str | None = None,
        awaiting_only: bool = False,
        now: datetime | None = None,
    ) -> list[ContactRow]:
        now = now or utcnow()
        contacts = self.session.scalars(self._query().order_by(Contact.id)).all()
        needle = (search or "").strip().lower()
        rows = []
        for contact in contacts:
            if (
                needle
                and needle
                not in " ".join(
                    [contact.name, contact.company.name, contact.job_title or ""]
                ).lower()
            ):
                continue
            state = thread_state(contact.interactions, now)
            if awaiting_only and not state.awaiting:
                continue
            rows.append(ContactRow(contact, state))
        # People you are waiting on first (longest wait first), then by latest activity.
        rows.sort(
            key=lambda r: (
                not r.state.awaiting,
                -(r.state.days_waiting or 0),
                -(r.state.last_activity.timestamp() if r.state.last_activity else 0),
            )
        )
        return rows

    def for_company(self, company_id: int, now: datetime | None = None) -> list[ContactRow]:
        now = now or utcnow()
        contacts = self.session.scalars(
            self._query().where(Contact.company_id == company_id).order_by(Contact.name)
        ).all()
        return [ContactRow(c, thread_state(c.interactions, now)) for c in contacts]

    def state(self, contact: Contact, now: datetime | None = None) -> ThreadState:
        return thread_state(contact.interactions, now or utcnow())

    # -- what happened -------------------------------------------------------------------

    def add_interaction(
        self,
        contact: Contact,
        kind: str = "message",
        *,
        direction: str | None = None,
        channel: str = "email",
        summary: str | None = None,
        at: datetime | None = None,
        application: Application | None = None,
        followup: Followup | None = None,
    ) -> Interaction:
        kind_value = Kind(kind)
        direction_value = Direction(direction) if direction else None
        check_interaction(kind_value, direction_value)
        if application is not None and application.company_id != contact.company_id:
            raise ValueError("That application is at a different company")
        interaction = Interaction(
            contact=contact,
            application=application,
            followup_id=followup.id if followup else None,
            kind=kind_value.value,
            direction=direction_value.value if direction_value else None,
            channel=Channel(channel).value,
            summary=_clean(summary),
            at=require_aware(at, "at") or utcnow(),
        )
        self.session.add(interaction)
        self.session.flush()
        return interaction

    def mark_replied(
        self,
        contact: Contact,
        *,
        at: datetime | None = None,
        application: Application | None = None,
    ) -> Interaction:
        """They answered: log an incoming message, on the channel you last wrote to them on."""
        sent = [
            i for i in contact.interactions if i.kind == "message" and i.direction == "outbound"
        ]
        if not sent:
            raise ValueError(
                "You have not written to this person yet, so there is nothing to answer"
            )
        return self.add_interaction(
            contact,
            "message",
            direction="inbound",
            channel=sent[-1].channel,
            summary="Replied",
            at=at,
            application=application or sent[-1].application,
        )


# -- finding people without scraping ---------------------------------------------------------


@dataclass(frozen=True)
class SearchLink:
    label: str
    url: str


def search_links(company: str, role: str | None = None) -> list[SearchLink]:
    """Search pages to open yourself. jobtrail never fetches them: you look, then you decide whom
    to add as a contact."""
    company = re.sub(r"\s+", " ", company.replace('"', " ")).strip()
    if not company:
        return []

    def linkedin(label: str, words: str) -> SearchLink:
        query = quote_plus(f"{company} {words}")
        return SearchLink(
            label, f"https://www.linkedin.com/search/results/people/?keywords={query}"
        )

    def google(label: str, words: str) -> SearchLink:
        query = quote_plus(f'site:linkedin.com/in "{company}" {words}')
        return SearchLink(label, f"https://www.google.com/search?q={query}")

    links = [
        linkedin("LinkedIn: recruiters and talent acquisition", "talent acquisition recruiter"),
        linkedin("LinkedIn: HR and people team", "HR manager people culture"),
        linkedin(
            "LinkedIn: selección y recursos humanos", "responsable selección recursos humanos"
        ),
    ]
    if role and role.strip():
        links.append(
            linkedin(
                f"LinkedIn: hiring manager for {role.strip()}", f"{role.strip()} hiring manager"
            )
        )
    links += [
        google("Google: recruiters on LinkedIn", 'recruiter OR "talent acquisition"'),
        SearchLink(
            "Google: the company's careers page",
            f"https://www.google.com/search?q={quote_plus(company + ' careers jobs')}",
        ),
    ]
    return links


@dataclass(frozen=True)
class Mentions:
    emails: list[str]
    profile_urls: list[str]


_EMAIL_IN_TEXT = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}")
_PROFILE_URL = re.compile(r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/in/[\w\-%]+/?", re.IGNORECASE)


def find_mentions(text: str) -> Mentions:
    """Emails and LinkedIn profile links that a posting states in so many words. Plain pattern
    matching, no model: what is returned is exactly what the text contains."""
    emails = list(dict.fromkeys(e.lower().rstrip(".") for e in _EMAIL_IN_TEXT.findall(text)))
    urls = list(dict.fromkeys(u.rstrip("/") for u in _PROFILE_URL.findall(text)))
    return Mentions(emails[:10], urls[:10])


# -- reminders -------------------------------------------------------------------------------


def local_today() -> date:
    return datetime.now().astimezone().date()


class ReminderService:
    def __init__(self, session: Session):
        self.session = session

    def add(
        self,
        *,
        due_on: date,
        note: str | None = None,
        application: Application | None = None,
        contact: Contact | None = None,
    ) -> Reminder:
        if application is None and contact is None:
            raise ValueError("A reminder needs an application or a contact")
        if (
            application is not None
            and contact is not None
            and application.company_id != contact.company_id
        ):
            raise ValueError("That contact works at a different company")
        reminder = Reminder(
            application=application, contact=contact, due_on=due_on, note=_clean(note)
        )
        self.session.add(reminder)
        self.session.flush()
        return reminder

    def add_in_days(self, days: int, *, today: date | None = None, **kwargs) -> Reminder:
        if not 0 <= days <= 365:
            raise ValueError("Choose between 0 and 365 days")
        return self.add(due_on=(today or local_today()) + timedelta(days=days), **kwargs)

    def get(self, reminder_id: int) -> Reminder:
        reminder = self.session.get(Reminder, reminder_id)
        if reminder is None:
            raise ReminderNotFound(reminder_id)
        return reminder

    def _open(self):
        return (
            select(Reminder)
            .options(
                selectinload(Reminder.application).selectinload(Application.company),
                selectinload(Reminder.contact).selectinload(Contact.company),
            )
            .where(Reminder.done_at.is_(None))
        )

    def due(self, today: date | None = None) -> list[Reminder]:
        today = today or local_today()
        query = self._open().where(Reminder.due_on <= today).order_by(Reminder.due_on, Reminder.id)
        return list(self.session.scalars(query))

    def upcoming(self, today: date | None = None, days: int = 7) -> list[Reminder]:
        today = today or local_today()
        query = (
            self._open()
            .where(Reminder.due_on > today, Reminder.due_on <= today + timedelta(days=days))
            .order_by(Reminder.due_on, Reminder.id)
        )
        return list(self.session.scalars(query))

    def open_for_application(self, application_id: int) -> list[Reminder]:
        query = (
            self._open().where(Reminder.application_id == application_id).order_by(Reminder.due_on)
        )
        return list(self.session.scalars(query))

    def open_for_contact(self, contact_id: int) -> list[Reminder]:
        query = self._open().where(Reminder.contact_id == contact_id).order_by(Reminder.due_on)
        return list(self.session.scalars(query))

    def complete(self, reminder: Reminder, at: datetime | None = None) -> None:
        reminder.done_at = require_aware(at, "at") or utcnow()
        self.session.flush()

    def snooze(self, reminder: Reminder, days: int, today: date | None = None) -> None:
        if not 1 <= days <= 365:
            raise ValueError("Snooze for between 1 and 365 days")
        reminder.due_on = (today or local_today()) + timedelta(days=days)
        reminder.done_at = None
        self.session.flush()

    def delete(self, reminder: Reminder) -> None:
        self.session.delete(reminder)
        self.session.flush()


def reminder_title(reminder: Reminder) -> str:
    """A one-line description, used on the page and in the Mac notification."""
    if reminder.note:
        return reminder.note
    if reminder.contact is not None:
        return f"Follow up with {reminder.contact.name} ({reminder.contact.company.name})"
    if reminder.application is not None:
        return f"Follow up on {reminder.application.company.name}"
    return "Follow up"


def reminder_url(reminder: Reminder) -> str:
    if reminder.contact_id is not None:
        return f"/contacts/{reminder.contact_id}"
    return f"/applications/{reminder.application_id}"
