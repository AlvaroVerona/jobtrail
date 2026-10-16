from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from jobtrail.database.models import (
    Application,
    Channel,
    Company,
    StatusEvent,
    Track,
    utcnow,
)
from jobtrail.domain.status import SUBMITTED_STATUSES, Status, check_transition

_EDITABLE_FIELDS = frozenset(
    {"role", "notes", "salary_expected", "salary_asked", "applied_at", "channel", "track"}
)


class ApplicationNotFound(LookupError):
    pass


class OptionError(ValueError):
    """A channel or track could not be renamed or deleted."""


def _require_aware(value: datetime | None, name: str) -> datetime | None:
    if value is not None and value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware (use UTC)")
    return value


class TrackerService:
    """All writes to applications go through here so the status history stays consistent."""

    def __init__(self, session: Session):
        self.session = session

    # -- lookups that create on first use ------------------------------------------------

    def get_or_create_company(self, name: str) -> Company:
        name = name.strip()
        if not name:
            raise ValueError("Company name cannot be empty")
        company = self.session.scalar(
            select(Company).where(func.lower(Company.name) == name.lower())
        )
        if company is None:
            company = Company(name=name)
            self.session.add(company)
            self.session.flush()
        return company

    def get_or_create_channel(self, name: str) -> Channel:
        return self._get_or_create_named(Channel, name)

    def get_or_create_track(self, name: str) -> Track:
        return self._get_or_create_named(Track, name)

    def _get_or_create_named(self, model, name: str):
        name = name.strip()
        if not name:
            raise ValueError(f"{model.__name__} name cannot be empty")
        row = self.session.scalar(select(model).where(func.lower(model.name) == name.lower()))
        if row is None:
            row = model(name=name)
            self.session.add(row)
            self.session.flush()
        return row

    # -- applications --------------------------------------------------------------------

    def create_application(
        self,
        company: str,
        role: str | None = None,
        *,
        status: Status = Status.WISHLIST,
        channel: str | None = None,
        track: str | None = None,
        applied_at: datetime | None = None,
        at: datetime | None = None,
        salary_expected: int | None = None,
        salary_asked: int | None = None,
        notes: str | None = None,
    ) -> Application:
        """Create an application in any starting status (an import may start mid-funnel).
        `at` is when it entered that status; it defaults to now."""
        at = _require_aware(at, "at") or utcnow()
        applied_at = _require_aware(applied_at, "applied_at")
        if applied_at is None and status in SUBMITTED_STATUSES:
            applied_at = at
        application = Application(
            company=self.get_or_create_company(company),
            role=role,
            status=status,
            channel=self.get_or_create_channel(channel) if channel else None,
            track=self.get_or_create_track(track) if track else None,
            applied_at=applied_at,
            salary_expected=salary_expected,
            salary_asked=salary_asked,
            notes=notes,
        )
        application.events.append(StatusEvent(from_status=None, to_status=status, at=at))
        self.session.add(application)
        self.session.flush()
        return application

    def get_application(self, application_id: int) -> Application:
        application = self.session.get(Application, application_id)
        if application is None:
            raise ApplicationNotFound(application_id)
        return application

    def list_applications(
        self,
        *,
        status: Status | None = None,
        track: str | None = None,
        channel: str | None = None,
        company: str | None = None,
        search: str | None = None,
    ) -> list[Application]:
        query = (
            select(Application)
            .join(Company)
            .options(
                selectinload(Application.company),
                selectinload(Application.channel),
                selectinload(Application.track),
                selectinload(Application.events),
            )
        )
        if status is not None:
            query = query.where(Application.status == status)
        if track is not None:
            query = query.join(Track, Application.track_id == Track.id).where(
                func.lower(Track.name) == track.lower()
            )
        if channel is not None:
            query = query.join(Channel, Application.channel_id == Channel.id).where(
                func.lower(Channel.name) == channel.lower()
            )
        if company is not None:
            query = query.where(func.lower(Company.name) == company.lower())
        if search:
            needle = search.strip().lower()
            query = query.where(
                func.lower(Company.name).contains(needle, autoescape=True)
                | func.lower(func.coalesce(Application.role, "")).contains(needle, autoescape=True)
            )
        return list(self.session.scalars(query.order_by(Application.id)))

    def update_application(self, application: Application, **fields) -> Application:
        """Edit descriptive fields. Status is deliberately not editable here: use change_status."""
        unknown = set(fields) - _EDITABLE_FIELDS
        if unknown:
            raise ValueError(f"Cannot edit: {', '.join(sorted(unknown))}")
        if "channel" in fields:
            name = fields.pop("channel")
            application.channel = self.get_or_create_channel(name) if name else None
        if "track" in fields:
            name = fields.pop("track")
            application.track = self.get_or_create_track(name) if name else None
        if "applied_at" in fields:
            _require_aware(fields["applied_at"], "applied_at")
        for key, value in fields.items():
            setattr(application, key, value)
        self.session.flush()
        return application

    def change_status(
        self,
        application: Application,
        to_status: Status,
        *,
        at: datetime | None = None,
        note: str | None = None,
    ) -> StatusEvent:
        """Move an application along the lifecycle, recording who/when in the history.

        `at` may be in the past (logging a reply late) but never before the previous event,
        because the funnel analytics assume events are in chronological order."""
        at = _require_aware(at, "at") or utcnow()
        check_transition(application.status, to_status)
        last = application.events[-1]
        if at < last.at:
            raise ValueError(
                f"Status change at {at.isoformat()} is before the previous event "
                f"({last.at.isoformat()})"
            )
        event = StatusEvent(from_status=application.status, to_status=to_status, at=at, note=note)
        application.events.append(event)
        application.status = to_status
        if to_status is Status.APPLIED and application.applied_at is None:
            application.applied_at = at
        self.session.flush()
        return event

    def history(self, application: Application) -> list[StatusEvent]:
        return list(application.events)

    def delete_application(self, application: Application) -> None:
        # A posting and its fit analyses stay useful without the application: just unlink.
        for posting in application.postings:
            posting.application_id = None
        self.session.delete(application)
        self.session.flush()

    # -- channel / track management ------------------------------------------------------

    def list_options(self, model) -> list[tuple[object, int]]:
        """(option, number of applications using it), alphabetical."""
        column = Application.channel_id if model is Channel else Application.track_id
        counts = dict(self.session.execute(select(column, func.count()).group_by(column)).all())
        options = self.session.scalars(select(model).order_by(func.lower(model.name)))
        return [(option, counts.get(option.id, 0)) for option in options]

    def rename_option(self, model, option_id: int, new_name: str) -> None:
        new_name = new_name.strip()
        if not new_name:
            raise OptionError("Name cannot be empty")
        option = self.session.get(model, option_id)
        if option is None:
            raise OptionError("Not found")
        clash = self.session.scalar(
            select(model).where(func.lower(model.name) == new_name.lower(), model.id != option_id)
        )
        if clash is not None:
            raise OptionError(f"'{clash.name}' already exists")
        option.name = new_name
        self.session.flush()

    def delete_option(self, model, option_id: int) -> None:
        option = self.session.get(model, option_id)
        if option is None:
            raise OptionError("Not found")
        used = dict((o.id, n) for o, n in self.list_options(model)).get(option_id, 0)
        if used:
            raise OptionError(f"'{option.name}' is used by {used} application(s)")
        self.session.delete(option)
        self.session.flush()
