from datetime import UTC, datetime

from sqlalchemy import (
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
