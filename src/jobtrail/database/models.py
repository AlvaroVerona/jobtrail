from datetime import UTC, date, datetime

from sqlalchemy import (
    JSON,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    TypeDecorator,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from jobtrail.domain.status import Status


def utcnow() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator):
    """Always timezone-aware UTC in Python, naive UTC in SQLite (which drops tzinfo)."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("Datetimes must be timezone-aware")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect):
        return None if value is None else value.replace(tzinfo=UTC)


def _status_column(**kwargs):
    return Enum(Status, values_callable=lambda e: [m.value for m in e], native_enum=False, **kwargs)


class Base(DeclarativeBase):
    pass


class Company(Base):
    __tablename__ = "company"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    website: Mapped[str | None] = mapped_column(String(500))
    notes: Mapped[str | None] = mapped_column(Text)

    applications: Mapped[list["Application"]] = relationship(back_populates="company")
    contacts: Mapped[list["Contact"]] = relationship(back_populates="company")


# One company per name, ignoring case ("ING" and "ing" are the same company).
Index("uq_company_name_ci", func.lower(Company.name), unique=True)


class Channel(Base):
    """How an application was submitted (Easy Apply, company website, referral...)."""

    __tablename__ = "channel"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)


class Track(Base):
    """The kind of role being targeted. User-defined, e.g. 'Data Science/AI'."""

    __tablename__ = "track"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)


class Application(Base):
    __tablename__ = "application"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("company.id"))
    role: Mapped[str | None] = mapped_column(String(300))
    status: Mapped[Status] = mapped_column(_status_column(), default=Status.WISHLIST)
    channel_id: Mapped[int | None] = mapped_column(ForeignKey("channel.id"))
    track_id: Mapped[int | None] = mapped_column(ForeignKey("track.id"))
    applied_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    salary_expected: Mapped[int | None] = mapped_column(Integer)
    salary_asked: Mapped[int | None] = mapped_column(Integer)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    company: Mapped[Company] = relationship(back_populates="applications")
    channel: Mapped[Channel | None] = relationship()
    track: Mapped[Track | None] = relationship()
    events: Mapped[list["StatusEvent"]] = relationship(
        back_populates="application",
        cascade="all, delete-orphan",
        order_by="StatusEvent.at, StatusEvent.id",
    )
    postings: Mapped[list["Posting"]] = relationship(back_populates="application")
    followups: Mapped[list["Followup"]] = relationship(
        back_populates="application",
        cascade="all, delete-orphan",
        order_by="Followup.sent_at, Followup.id",
    )
    interactions: Mapped[list["Interaction"]] = relationship(back_populates="application")
    reminders: Mapped[list["Reminder"]] = relationship(back_populates="application")


class StatusEvent(Base):
    """One row per status change. The current status is a cache of the latest event;
    funnel analytics read from here."""

    __tablename__ = "status_event"

    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int] = mapped_column(ForeignKey("application.id"))
    from_status: Mapped[Status | None] = mapped_column(_status_column())
    to_status: Mapped[Status] = mapped_column(_status_column())
    at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    note: Mapped[str | None] = mapped_column(Text)

    application: Mapped[Application] = relationship(back_populates="events")


class Followup(Base):
    """A message actually sent about an application: a follow-up, a thank-you, an intro.
    Unlike a status, it can be repeated, and each one restarts the 'waiting' clock."""

    __tablename__ = "followup"

    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int] = mapped_column(ForeignKey("application.id"))
    kind: Mapped[str] = mapped_column(String(30))
    sent_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    text: Mapped[str | None] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)

    application: Mapped[Application] = relationship(back_populates="followups")


class Contact(Base):
    """A person at a company you are in touch with (or want to be)."""

    __tablename__ = "contact"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("company.id"))
    name: Mapped[str] = mapped_column(String(200))
    job_title: Mapped[str | None] = mapped_column(String(200))
    profile_url: Mapped[str | None] = mapped_column(String(1000))
    email: Mapped[str | None] = mapped_column(String(320))
    source: Mapped[str] = mapped_column(String(20), default="other")
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    company: Mapped[Company] = relationship(back_populates="contacts")
    interactions: Mapped[list["Interaction"]] = relationship(
        back_populates="contact",
        cascade="all, delete-orphan",
        order_by="Interaction.at, Interaction.id",
    )
    reminders: Mapped[list["Reminder"]] = relationship(
        back_populates="contact", cascade="all, delete-orphan"
    )


class Interaction(Base):
    """One exchange with a contact: a message sent or received, a call, a meeting, a note.
    A reply is an inbound message, which is how the app knows someone answered."""

    __tablename__ = "interaction"

    id: Mapped[int] = mapped_column(primary_key=True)
    contact_id: Mapped[int] = mapped_column(ForeignKey("contact.id"))
    application_id: Mapped[int | None] = mapped_column(ForeignKey("application.id"))
    followup_id: Mapped[int | None] = mapped_column(ForeignKey("followup.id"))
    kind: Mapped[str] = mapped_column(String(10), default="message")
    direction: Mapped[str | None] = mapped_column(String(10))
    channel: Mapped[str] = mapped_column(String(20), default="email")
    summary: Mapped[str | None] = mapped_column(Text)
    at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    contact: Mapped[Contact] = relationship(back_populates="interactions")
    application: Mapped["Application | None"] = relationship(back_populates="interactions")


class Reminder(Base):
    """'Look at this again on that day.' Attached to an application, a contact, or both."""

    __tablename__ = "reminder"

    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int | None] = mapped_column(ForeignKey("application.id"))
    contact_id: Mapped[int | None] = mapped_column(ForeignKey("contact.id"))
    due_on: Mapped[date] = mapped_column(Date)
    note: Mapped[str | None] = mapped_column(String(300))
    done_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    application: Mapped["Application | None"] = relationship(back_populates="reminders")
    contact: Mapped[Contact | None] = relationship(back_populates="reminders")


class CVVersion(Base):
    """One version of the user's CV, as extracted text. Deduplicated by content hash."""

    __tablename__ = "cv_version"

    id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str] = mapped_column(String(200))
    content_hash: Mapped[str] = mapped_column(String(64), unique=True)
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    fits: Mapped[list["FitAnalysis"]] = relationship(
        back_populates="cv_version", cascade="all, delete-orphan"
    )


class Posting(Base):
    """A job posting the user pasted in, with the requirements extracted from it."""

    __tablename__ = "posting"

    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int | None] = mapped_column(ForeignKey("application.id"))
    url: Mapped[str | None] = mapped_column(String(1000))
    raw_text: Mapped[str] = mapped_column(Text)
    parsed: Mapped[dict] = mapped_column(JSON)
    model: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    application: Mapped[Application | None] = relationship(back_populates="postings")
    fits: Mapped[list["FitAnalysis"]] = relationship(
        back_populates="posting",
        cascade="all, delete-orphan",
        order_by="FitAnalysis.created_at.desc(), FitAnalysis.id.desc()",
    )


class FitAnalysis(Base):
    """How well one CV version matches one posting. `detail` holds the per-requirement
    verdicts, including the verbatim CV quote behind each one."""

    __tablename__ = "fit_analysis"

    id: Mapped[int] = mapped_column(primary_key=True)
    posting_id: Mapped[int] = mapped_column(ForeignKey("posting.id"))
    cv_version_id: Mapped[int] = mapped_column(ForeignKey("cv_version.id"))
    score: Mapped[int | None] = mapped_column(Integer)
    detail: Mapped[dict] = mapped_column(JSON)
    model: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    posting: Mapped[Posting] = relationship(back_populates="fits")
    cv_version: Mapped[CVVersion] = relationship(back_populates="fits")
