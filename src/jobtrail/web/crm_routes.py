"""Contacts, what was said, and reminders.

All of it is typed in by you: jobtrail does not look people up, send anything or read a mailbox."""

from datetime import date, datetime
from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request

from jobtrail.database.models import Contact, Reminder, utcnow
from jobtrail.domain.outreach import LABELS as OUTREACH_LABELS
from jobtrail.domain.outreach import Channel, Kind, Source
from jobtrail.services.contacts import (
    ContactNotFound,
    ContactService,
    ReminderNotFound,
    ReminderService,
    local_today,
    reminder_title,
    reminder_url,
)
from jobtrail.services.tracker import ApplicationNotFound, TrackerService
from jobtrail.web.routes import SessionDep, parse_date, redirect, render, safe_next

router = APIRouter()

JOB_TITLE_SUGGESTIONS = [
    "Talent Acquisition",
    "Recruiter",
    "HR Manager",
    "Hiring Manager",
    "People & Culture",
    "Responsable de Selección",
]


def _contact_or_404(service: ContactService, contact_id: int) -> Contact:
    try:
        return service.get(contact_id)
    except ContactNotFound:
        raise HTTPException(404, "Contact not found") from None


def _reminder_or_404(service: ReminderService, reminder_id: int) -> Reminder:
    try:
        return service.get(reminder_id)
    except ReminderNotFound:
        raise HTTPException(404, "Reminder not found") from None


def _at(text: str) -> datetime | None:
    """A picked day becomes 'now' if it is today, otherwise midnight UTC."""
    picked = parse_date(text)
    if picked is None:
        return None
    return utcnow() if picked.date() == date.today() else picked


def _int_or_none(text: str, label: str) -> int | None:
    text = (text or "").strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        raise ValueError(f"{label} is not valid") from None


# -- contacts --------------------------------------------------------------------------------


def _form_context(tracker: TrackerService) -> dict:
    return {
        "company_names": [c.name for c in tracker.list_companies()],
        "title_suggestions": JOB_TITLE_SUGGESTIONS,
        "sources": [(s.value, OUTREACH_LABELS[s.value]) for s in Source],
    }


@router.get("/contacts")
def contacts_list(request: Request, session: SessionDep, q: str = "", awaiting: str = ""):
    rows = ContactService(session).list_contacts(search=q or None, awaiting_only=bool(awaiting))
    return render(
        request,
        "contacts.html",
        rows=rows,
        filters={"q": q, "awaiting": bool(awaiting)},
        labels=OUTREACH_LABELS,
    )


@router.get("/contacts/new")
def contact_new(request: Request, session: SessionDep):
    params = request.query_params
    values = {
        key: params.get(key, "")
        for key in (
            "company",
            "name",
            "job_title",
            "email",
            "profile_url",
            "source",
            "application_id",
        )
    }
    return render(
        request,
        "contact_new.html",
        values=values,
        error=None,
        **_form_context(TrackerService(session)),
    )


@router.post("/contacts")
def contact_create(
    request: Request,
    session: SessionDep,
    company: Annotated[str, Form()] = "",
    name: Annotated[str, Form()] = "",
    job_title: Annotated[str, Form()] = "",
    profile_url: Annotated[str, Form()] = "",
    email: Annotated[str, Form()] = "",
    source: Annotated[str, Form()] = "other",
    notes: Annotated[str, Form()] = "",
    application_id: Annotated[str, Form()] = "",
):
    values = {
        "company": company,
        "name": name,
        "job_title": job_title,
        "profile_url": profile_url,
        "email": email,
        "source": source,
        "notes": notes,
        "application_id": application_id,
    }
    try:
        contact = ContactService(session).create(
            company,
            name,
            job_title=job_title,
            profile_url=profile_url,
            email=email,
            source=source,
            notes=notes,
        )
        session.commit()
    except ValueError as exc:
        session.rollback()
        return render(
            request,
            "contact_new.html",
            400,
            values=values,
            error=str(exc),
            **_form_context(TrackerService(session)),
        )
    return redirect(f"/contacts/{contact.id}")


def _contact_page(request, session, contact, *, error=None, status_code=200):
    service = ContactService(session)
    tracker = TrackerService(session)
    return render(
        request,
        "contact_detail.html",
        status_code,
        contact=contact,
        state=service.state(contact),
        timeline=sorted(contact.interactions, key=lambda i: (i.at, i.id), reverse=True),
        applications=tracker.list_applications(company=contact.company.name),
        reminders=ReminderService(session).open_for_contact(contact.id),
        error=error,
        labels=OUTREACH_LABELS,
        kinds=[(k.value, OUTREACH_LABELS[k.value]) for k in Kind],
        channels=[(c.value, OUTREACH_LABELS[c.value]) for c in Channel],
        today=local_today(),
        **_form_context(tracker),
    )


@router.get("/contacts/{contact_id}")
def contact_detail(request: Request, contact_id: int, session: SessionDep):
    contact = _contact_or_404(ContactService(session), contact_id)
    return _contact_page(request, session, contact)


@router.post("/contacts/{contact_id}/edit")
def contact_edit(
    request: Request,
    contact_id: int,
    session: SessionDep,
    name: Annotated[str, Form()] = "",
    job_title: Annotated[str, Form()] = "",
    profile_url: Annotated[str, Form()] = "",
    email: Annotated[str, Form()] = "",
    source: Annotated[str, Form()] = "other",
    notes: Annotated[str, Form()] = "",
):
    service = ContactService(session)
    contact = _contact_or_404(service, contact_id)
    try:
        service.update(
            contact,
            name=name,
            job_title=job_title,
            profile_url=profile_url,
            email=email,
            source=source,
            notes=notes,
        )
        session.commit()
    except ValueError as exc:
        session.rollback()
        return _contact_page(request, session, contact, error=str(exc), status_code=400)
    return redirect(f"/contacts/{contact_id}")


@router.post("/contacts/{contact_id}/delete")
def contact_delete(contact_id: int, session: SessionDep):
    service = ContactService(session)
    service.delete(_contact_or_404(service, contact_id))
    session.commit()
    return redirect("/contacts")


@router.post("/contacts/{contact_id}/interactions")
def interaction_add(
    request: Request,
    contact_id: int,
    session: SessionDep,
    kind: Annotated[str, Form()] = "message",
    direction: Annotated[str, Form()] = "",
    channel: Annotated[str, Form()] = "email",
    summary: Annotated[str, Form()] = "",
    date_: Annotated[str, Form(alias="date")] = "",
    application_id: Annotated[str, Form()] = "",
):
    service = ContactService(session)
    contact = _contact_or_404(service, contact_id)
    try:
        linked_id = _int_or_none(application_id, "The application")
        application = TrackerService(session).get_application(linked_id) if linked_id else None
        service.add_interaction(
            contact,
            kind,
            direction=direction or None,
            channel=channel,
            summary=summary,
            at=_at(date_),
            application=application,
        )
        session.commit()
    except (ValueError, ApplicationNotFound) as exc:
        session.rollback()
        message = (
            "That application does not exist" if isinstance(exc, ApplicationNotFound) else str(exc)
        )
        return _contact_page(request, session, contact, error=message, status_code=400)
    return redirect(f"/contacts/{contact_id}")


@router.post("/contacts/{contact_id}/replied")
def contact_replied(
    request: Request,
    contact_id: int,
    session: SessionDep,
    next: Annotated[str, Form()] = "",
):
    service = ContactService(session)
    contact = _contact_or_404(service, contact_id)
    try:
        service.mark_replied(contact)
        session.commit()
    except ValueError as exc:
        session.rollback()
        return _contact_page(request, session, contact, error=str(exc), status_code=400)
    return redirect(safe_next(next, f"/contacts/{contact_id}"))


# -- reminders -------------------------------------------------------------------------------


@router.post("/reminders")
def reminder_add(
    request: Request,
    session: SessionDep,
    application_id: Annotated[str, Form()] = "",
    contact_id: Annotated[str, Form()] = "",
    days: Annotated[str, Form()] = "",
    due_on: Annotated[str, Form()] = "",
    note: Annotated[str, Form()] = "",
    next: Annotated[str, Form()] = "",
):
    contacts = ContactService(session)
    tracker = TrackerService(session)
    reminders = ReminderService(session)
    fallback = "/followups"
    try:
        app_id = _int_or_none(application_id, "The application")
        con_id = _int_or_none(contact_id, "The contact")
        application = tracker.get_application(app_id) if app_id else None
        contact = contacts.get(con_id) if con_id else None
        fallback = f"/contacts/{con_id}" if con_id else f"/applications/{app_id}"
        if due_on.strip():
            picked = parse_date(due_on)
            reminders.add(due_on=picked.date(), note=note, application=application, contact=contact)
        else:
            reminders.add_in_days(
                _int_or_none(days, "The number of days") or 0,
                note=note,
                application=application,
                contact=contact,
            )
        session.commit()
    except (ValueError, ApplicationNotFound, ContactNotFound) as exc:
        session.rollback()
        return render(request, "error.html", 400, message=f"Could not set the reminder: {exc}")
    return redirect(safe_next(next, fallback))


@router.post("/reminders/{reminder_id}/done")
def reminder_done(session: SessionDep, reminder_id: int, next: Annotated[str, Form()] = ""):
    service = ReminderService(session)
    service.complete(_reminder_or_404(service, reminder_id))
    session.commit()
    return redirect(safe_next(next, "/followups"))


@router.post("/reminders/{reminder_id}/snooze")
def reminder_snooze(
    request: Request,
    session: SessionDep,
    reminder_id: int,
    days: Annotated[int, Form()] = 3,
    next: Annotated[str, Form()] = "",
):
    service = ReminderService(session)
    reminder = _reminder_or_404(service, reminder_id)
    try:
        service.snooze(reminder, days)
        session.commit()
    except ValueError as exc:
        session.rollback()
        return render(request, "error.html", 400, message=str(exc))
    return redirect(safe_next(next, "/followups"))


@router.post("/reminders/{reminder_id}/delete")
def reminder_delete(session: SessionDep, reminder_id: int, next: Annotated[str, Form()] = ""):
    service = ReminderService(session)
    service.delete(_reminder_or_404(service, reminder_id))
    session.commit()
    return redirect(safe_next(next, "/followups"))


@router.get("/api/reminders")
def reminders_api(session: SessionDep) -> dict:
    """What is due today or overdue, for the macOS app's Dock badge and notifications."""
    due = ReminderService(session).due(local_today())
    return {
        "due": len(due),
        "items": [
            {
                "id": r.id,
                "title": reminder_title(r),
                "due_on": r.due_on.isoformat(),
                "url": reminder_url(r),
            }
            for r in due[:50]
        ],
    }
