"""Server-rendered pages. Every page works as a plain HTML form; HTMX (hx-boost) only makes
navigation smoother. Handlers commit explicitly so a redirect never races its own write."""

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException as StarletteHTTPException

from jobtrail.database.models import Application, Channel, Track, utcnow
from jobtrail.domain.followup import ALLOWED_STATUSES, LABELS, FollowupKind
from jobtrail.domain.status import ALLOWED_TRANSITIONS, Status
from jobtrail.services.analytics import AnalyticsService
from jobtrail.services.matching import CVService
from jobtrail.services.message_drafter import LANGUAGES
from jobtrail.services.tracker import ApplicationNotFound, TrackerService

router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

STATUS_ORDER = list(Status)
BOARD_STATUSES = [
    Status.WISHLIST,
    Status.TO_APPLY,
    Status.APPLIED,
    Status.FOLLOW_UP_SENT,
    Status.SCREENING,
    Status.INTERVIEWING,
    Status.OFFER,
]
CLOSED_STATUSES = [Status.REJECTED, Status.NO_RESPONSE, Status.WITHDRAWN]
CREATABLE_STATUSES = [Status.WISHLIST, Status.TO_APPLY, Status.APPLIED]
OPTION_MODELS = {"channels": Channel, "tracks": Track}


# -- template helpers --------------------------------------------------------------------------


def status_label(status: Status | str) -> str:
    value = status.value if isinstance(status, Status) else status
    return {"follow_up_sent": "Follow-up sent"}.get(value, value.replace("_", " ").capitalize())


def fmt_date(value: datetime | None) -> str:
    return f"{value.day} {value:%b %Y}" if value else ""


def days_ago(value: datetime | None) -> int | None:
    return (utcnow() - value).days if value else None


def pct(value: float | None) -> str:
    return "-" if value is None else f"{value:.0%}"


def allowed_targets(status: Status) -> list[Status]:
    return sorted(ALLOWED_TRANSITIONS[status], key=STATUS_ORDER.index)


templates.env.filters.update(
    status_label=status_label, fmt_date=fmt_date, days_ago=days_ago, pct=pct
)
templates.env.globals.update(allowed_targets=allowed_targets, STATUS_ORDER=STATUS_ORDER)


def render(request: Request, name: str, status_code: int = 200, **context) -> HTMLResponse:
    context["path"] = request.url.path
    return templates.TemplateResponse(request, name, context, status_code=status_code)


def register_error_pages(app) -> None:
    @app.exception_handler(StarletteHTTPException)
    async def html_error(request: Request, exc: StarletteHTTPException):
        if exc.status_code == 404 and not request.url.path.startswith("/static"):
            return render(request, "error.html", 404, message="Page not found")
        return HTMLResponse(str(exc.detail), status_code=exc.status_code)


# -- request plumbing --------------------------------------------------------------------------


def get_session(request: Request) -> Iterator[Session]:
    with request.app.state.session_factory() as session:
        yield session


SessionDep = Annotated[Session, Depends(get_session)]


def get_application(tracker: TrackerService, application_id: int) -> Application:
    try:
        return tracker.get_application(application_id)
    except ApplicationNotFound:
        raise HTTPException(404, "Application not found") from None


def parse_date(text: str) -> datetime | None:
    text = text.strip()
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError:
        raise ValueError(f"'{text}' is not a valid date (use YYYY-MM-DD)") from None


def parse_int(text: str, label: str) -> int | None:
    text = text.strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        raise ValueError(f"{label} must be a whole number") from None


def resolve_at(day: datetime | None, last_event_at: datetime, now: datetime) -> datetime | None:
    """Turn a picked day into a timestamp that keeps the history in order.

    A date picker has no time of day. Today means 'now'; the same day as the previous event
    means 'not before that event'; any other day is midnight UTC."""
    if day is None:
        return None
    if day.date() == now.date():
        return now
    if day.date() == last_event_at.date() and day < last_event_at:
        return last_event_at
    return day


def safe_next(target: str, default: str) -> str:
    """Only allow redirects to a path on this site (never to another host)."""
    if target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return default


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def option_names(tracker: TrackerService) -> dict:
    return {
        "channel_names": [o.name for o, _ in tracker.list_options(Channel)],
        "track_names": [o.name for o, _ in tracker.list_options(Track)],
    }


# -- pages -------------------------------------------------------------------------------------


@router.get("/")
def home():
    return redirect("/board")


@router.get("/board")
def board(request: Request, session: SessionDep, track: str = ""):
    tracker = TrackerService(session)
    applications = tracker.list_applications(track=track or None)

    def column(status: Status) -> list[Application]:
        items = [a for a in applications if a.status is status]
        waiting = status in (Status.APPLIED, Status.FOLLOW_UP_SENT)
        return sorted(items, key=lambda a: a.events[-1].at, reverse=not waiting)

    return render(
        request,
        "board.html",
        columns=[(s, column(s)) for s in BOARD_STATUSES],
        closed=[(s, column(s)) for s in CLOSED_STATUSES],
        selected_track=track,
        **option_names(tracker),
    )


@router.get("/applications")
def applications_list(
    request: Request,
    session: SessionDep,
    q: str = "",
    status: str = "",
    track: str = "",
    channel: str = "",
):
    tracker = TrackerService(session)
    wanted = Status(status) if status in Status._value2member_map_ else None
    rows = tracker.list_applications(
        status=wanted, track=track or None, channel=channel or None, search=q or None
    )
    rows.sort(
        key=lambda a: (a.applied_at is None, -(a.applied_at.timestamp() if a.applied_at else 0))
    )
    return render(
        request,
        "applications.html",
        rows=rows,
        filters={"q": q, "status": status, "track": track, "channel": channel},
        **option_names(tracker),
    )


def _form_values(**fields: str) -> dict[str, str]:
    return {key: value for key, value in fields.items()}


@router.get("/applications/new")
def new_application_form(request: Request, session: SessionDep):
    return render(
        request,
        "application_new.html",
        values={"status": "wishlist"},
        error=None,
        creatable=CREATABLE_STATUSES,
        **option_names(TrackerService(session)),
    )


@router.post("/applications")
def create_application(
    request: Request,
    session: SessionDep,
    company: Annotated[str, Form()] = "",
    role: Annotated[str, Form()] = "",
    status: Annotated[str, Form()] = "wishlist",
    applied_date: Annotated[str, Form()] = "",
    channel: Annotated[str, Form()] = "",
    track: Annotated[str, Form()] = "",
    salary_expected: Annotated[str, Form()] = "",
    salary_asked: Annotated[str, Form()] = "",
    notes: Annotated[str, Form()] = "",
):
    tracker = TrackerService(session)
    values = _form_values(
        company=company,
        role=role,
        status=status,
        applied_date=applied_date,
        channel=channel,
        track=track,
        salary_expected=salary_expected,
        salary_asked=salary_asked,
        notes=notes,
    )
    try:
        chosen = Status(status)
        if chosen not in CREATABLE_STATUSES:
            raise ValueError("A new application can only start as wishlist, to apply or applied")
        day = parse_date(applied_date) if chosen is Status.APPLIED else None
        now = utcnow()
        at = resolve_at(day, now, now) if day else None
        application = tracker.create_application(
            company,
            role.strip() or None,
            status=chosen,
            channel=channel.strip() or None,
            track=track.strip() or None,
            at=at,
            salary_expected=parse_int(salary_expected, "Expected salary"),
            salary_asked=parse_int(salary_asked, "Asked salary"),
            notes=notes.strip() or None,
        )
        session.commit()
    except ValueError as exc:
        session.rollback()
        return render(
            request,
            "application_new.html",
            400,
            values=values,
            error=str(exc),
            creatable=CREATABLE_STATUSES,
            **option_names(tracker),
        )
    return redirect(f"/applications/{application.id}")


def _detail(request, tracker, application, *, error=None, status_code=200, draft=None):
    kinds = [
        (k.value, LABELS[k]) for k in FollowupKind if application.status in ALLOWED_STATUSES[k]
    ]
    return render(
        request,
        "application_detail.html",
        status_code,
        app=application,
        history=tracker.history(application),
        error=error,
        draft=draft,
        kinds=kinds,
        kind_labels={k.value: LABELS[k] for k in FollowupKind},
        languages=LANGUAGES,
        versions=CVService(tracker.session).list(),
        **option_names(tracker),
    )


@router.get("/applications/{application_id}")
def application_detail(request: Request, application_id: int, session: SessionDep):
    tracker = TrackerService(session)
    return _detail(request, tracker, get_application(tracker, application_id))


@router.post("/applications/{application_id}/status")
def change_status(
    request: Request,
    application_id: int,
    session: SessionDep,
    to_status: Annotated[str, Form()],
    date: Annotated[str, Form()] = "",
    note: Annotated[str, Form()] = "",
    next: Annotated[str, Form()] = "",
):
    tracker = TrackerService(session)
    application = get_application(tracker, application_id)
    try:
        target = Status(to_status)
        now = utcnow()
        at = resolve_at(parse_date(date), application.events[-1].at, now)
        tracker.change_status(application, target, at=at, note=note.strip() or None)
        session.commit()
    except ValueError as exc:
        session.rollback()
        return _detail(request, tracker, application, error=str(exc), status_code=400)
    return redirect(safe_next(next, f"/applications/{application_id}"))


@router.post("/applications/{application_id}/followup")
def record_followup(
    request: Request,
    application_id: int,
    session: SessionDep,
    kind: Annotated[str, Form()] = "follow_up",
    subject: Annotated[str, Form()] = "",
    text: Annotated[str, Form()] = "",
    note: Annotated[str, Form()] = "",
    next: Annotated[str, Form()] = "",
):
    tracker = TrackerService(session)
    application = get_application(tracker, application_id)
    body = text.strip()
    full_text = f"Subject: {subject.strip()}\n\n{body}" if subject.strip() and body else body
    try:
        tracker.record_followup(
            application, FollowupKind(kind), text=full_text or None, note=note.strip() or None
        )
        session.commit()
    except ValueError as exc:
        session.rollback()
        return _detail(request, tracker, application, error=str(exc), status_code=400)
    return redirect(safe_next(next, f"/applications/{application_id}#outreach"))


@router.post("/applications/{application_id}/edit")
def edit_application(
    request: Request,
    application_id: int,
    session: SessionDep,
    role: Annotated[str, Form()] = "",
    applied_date: Annotated[str, Form()] = "",
    channel: Annotated[str, Form()] = "",
    track: Annotated[str, Form()] = "",
    salary_expected: Annotated[str, Form()] = "",
    salary_asked: Annotated[str, Form()] = "",
    notes: Annotated[str, Form()] = "",
):
    tracker = TrackerService(session)
    application = get_application(tracker, application_id)
    try:
        tracker.update_application(
            application,
            role=role.strip() or None,
            applied_at=parse_date(applied_date),
            channel=channel.strip() or None,
            track=track.strip() or None,
            salary_expected=parse_int(salary_expected, "Expected salary"),
            salary_asked=parse_int(salary_asked, "Asked salary"),
            notes=notes.strip() or None,
        )
        session.commit()
    except ValueError as exc:
        session.rollback()
        return _detail(request, tracker, application, error=str(exc), status_code=400)
    return redirect(f"/applications/{application_id}")


@router.post("/applications/{application_id}/delete")
def delete_application(application_id: int, session: SessionDep):
    tracker = TrackerService(session)
    tracker.delete_application(get_application(tracker, application_id))
    session.commit()
    return redirect("/applications")


@router.get("/followups")
def followups(request: Request, session: SessionDep):
    settings = request.app.state.settings
    analytics = AnalyticsService(
        session, min_sample=settings.min_sample, stale_after_days=settings.stale_after_days
    )
    return render(
        request,
        "followups.html",
        stale=analytics.stale_applications(),
        stale_after_days=settings.stale_after_days,
        Status=Status,
    )


@router.get("/analytics")
def analytics_page(request: Request, session: SessionDep):
    settings = request.app.state.settings
    analytics = AnalyticsService(
        session, min_sample=settings.min_sample, stale_after_days=settings.stale_after_days
    )
    report = analytics.report()
    peak = max((count for _, count in report.weekly_applications), default=0)
    return render(
        request, "analytics.html", report=report, min_sample=settings.min_sample, peak=peak
    )


def _settings_page(request, tracker, *, error=None, status_code=200):
    return render(
        request,
        "settings.html",
        status_code,
        channels=tracker.list_options(Channel),
        tracks=tracker.list_options(Track),
        settings=request.app.state.settings,
        error=error,
    )


@router.get("/settings")
def settings_page(request: Request, session: SessionDep):
    return _settings_page(request, TrackerService(session))


def _option_model(kind: str):
    try:
        return OPTION_MODELS[kind]
    except KeyError:
        raise HTTPException(404, "Unknown option type") from None


@router.post("/settings/{kind}")
def add_option(request: Request, kind: str, session: SessionDep, name: Annotated[str, Form()] = ""):
    model = _option_model(kind)
    tracker = TrackerService(session)
    try:
        if model is Channel:
            tracker.get_or_create_channel(name)
        else:
            tracker.get_or_create_track(name)
        session.commit()
    except ValueError as exc:
        session.rollback()
        return _settings_page(request, tracker, error=str(exc), status_code=400)
    return redirect("/settings")


@router.post("/settings/{kind}/{option_id}/rename")
def rename_option(
    request: Request,
    kind: str,
    option_id: int,
    session: SessionDep,
    name: Annotated[str, Form()] = "",
):
    model = _option_model(kind)
    tracker = TrackerService(session)
    try:
        tracker.rename_option(model, option_id, name)
        session.commit()
    except ValueError as exc:
        session.rollback()
        return _settings_page(request, tracker, error=str(exc), status_code=400)
    return redirect("/settings")


@router.post("/settings/{kind}/{option_id}/delete")
def delete_option(request: Request, kind: str, option_id: int, session: SessionDep):
    model = _option_model(kind)
    tracker = TrackerService(session)
    try:
        tracker.delete_option(model, option_id)
        session.commit()
    except ValueError as exc:
        session.rollback()
        return _settings_page(request, tracker, error=str(exc), status_code=400)
    return redirect("/settings")
