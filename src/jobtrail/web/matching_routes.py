"""CV versions, postings and fit analysis pages.

The actions that call the local model (adding a posting, analysing fit) are plain form
posts rather than htmx ones, so the browser shows its own loading indicator while a
local model works for a while."""

from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile

from jobtrail.domain.followup import FollowupKind
from jobtrail.ingestion.cv import CVError
from jobtrail.llm.base import LLMClient, LLMError, LLMUnavailable
from jobtrail.services.matching import CVService, MatchingService, NotFound
from jobtrail.services.message_drafter import MessageDrafter
from jobtrail.services.tracker import ApplicationNotFound, TrackerService
from jobtrail.web.routes import SessionDep, _detail, get_application, redirect, render

router = APIRouter()


def get_llm(request: Request) -> LLMClient:
    return request.app.state.llm_factory()


def _or_404(call, *args):
    try:
        return call(*args)
    except NotFound:
        raise HTTPException(404, "Not found") from None


# -- CV versions ------------------------------------------------------------------------------


def _cv_page(request, session, *, error=None, notice=None, status_code=200):
    return render(
        request,
        "cv.html",
        status_code,
        versions=CVService(session).list(),
        error=error,
        notice=notice,
    )


@router.get("/cv")
def cv_list(request: Request, session: SessionDep):
    return _cv_page(request, session)


@router.post("/cv")
async def cv_add(
    request: Request,
    session: SessionDep,
    label: Annotated[str, Form()] = "",
    text: Annotated[str, Form()] = "",
    file: Annotated[UploadFile | None, File()] = None,
):
    service = CVService(session)
    try:
        if file is not None and file.filename:
            version, created = service.add_file(label, file.filename, await file.read())
        else:
            version, created = service.add_text(label or "Pasted CV", text)
        session.commit()
    except CVError as exc:
        session.rollback()
        return _cv_page(request, session, error=str(exc), status_code=400)
    notice = None if created else f"This CV is already saved as '{version.label}'."
    return _cv_page(request, session, notice=notice)


@router.get("/cv/{cv_id}")
def cv_detail(request: Request, cv_id: int, session: SessionDep):
    version = _or_404(CVService(session).get, cv_id)
    return render(request, "cv_detail.html", version=version)


@router.post("/cv/{cv_id}/delete")
def cv_delete(cv_id: int, session: SessionDep):
    service = CVService(session)
    service.delete(_or_404(service.get, cv_id))
    session.commit()
    return redirect("/cv")


# -- postings ---------------------------------------------------------------------------------


def _posting_form(request, session, *, values=None, error=None, status_code=200):
    llm = get_llm(request)
    return render(
        request,
        "posting_new.html",
        status_code,
        values=values or {"application_id": request.query_params.get("application_id", "")},
        error=error,
        llm_problem=llm.check(),
    )


@router.get("/postings")
def postings_list(request: Request, session: SessionDep):
    service = MatchingService(session, get_llm(request))
    return render(request, "postings.html", postings=service.list_postings())


@router.get("/postings/new")
def posting_new(request: Request, session: SessionDep):
    return _posting_form(request, session)


@router.post("/postings")
def posting_create(
    request: Request,
    session: SessionDep,
    raw_text: Annotated[str, Form()] = "",
    url: Annotated[str, Form()] = "",
    application_id: Annotated[str, Form()] = "",
):
    values = {"raw_text": raw_text, "url": url, "application_id": application_id}
    service = MatchingService(session, get_llm(request))
    try:
        linked = int(application_id) if application_id.strip() else None
        posting = service.add_posting(raw_text, url or None, linked)
        session.commit()
    except LLMUnavailable as exc:
        session.rollback()
        return _posting_form(request, session, values=values, error=str(exc), status_code=503)
    except LLMError as exc:
        session.rollback()
        return _posting_form(request, session, values=values, error=str(exc), status_code=502)
    except (ValueError, ApplicationNotFound) as exc:
        session.rollback()
        message = (
            "That application does not exist" if isinstance(exc, ApplicationNotFound) else str(exc)
        )
        return _posting_form(request, session, values=values, error=message, status_code=400)
    return redirect(f"/postings/{posting.id}")


def _posting_page(request, session, posting, *, error=None, status_code=200):
    return render(
        request,
        "posting_detail.html",
        status_code,
        posting=posting,
        parsed=posting.parsed,
        versions=CVService(session).list(),
        error=error,
        llm_problem=get_llm(request).check(),
    )


@router.get("/postings/{posting_id}")
def posting_detail(request: Request, posting_id: int, session: SessionDep):
    posting = _or_404(MatchingService(session, get_llm(request)).get_posting, posting_id)
    return _posting_page(request, session, posting)


@router.post("/postings/{posting_id}/fit")
def posting_fit(
    request: Request,
    posting_id: int,
    session: SessionDep,
    cv_version_id: Annotated[int, Form()],
):
    service = MatchingService(session, get_llm(request))
    posting = _or_404(service.get_posting, posting_id)
    cv = _or_404(CVService(session).get, cv_version_id)
    try:
        service.analyze(posting, cv)
        session.commit()
    except LLMUnavailable as exc:
        session.rollback()
        return _posting_page(request, session, posting, error=str(exc), status_code=503)
    except (LLMError, ValueError) as exc:
        session.rollback()
        return _posting_page(request, session, posting, error=str(exc), status_code=502)
    return redirect(f"/postings/{posting_id}")


@router.post("/postings/{posting_id}/delete")
def posting_delete(request: Request, posting_id: int, session: SessionDep):
    service = MatchingService(session, get_llm(request))
    service.delete_posting(_or_404(service.get_posting, posting_id))
    session.commit()
    return redirect("/postings")


@router.post("/postings/{posting_id}/fit/{fit_id}/advice")
def posting_advice(request: Request, posting_id: int, fit_id: int, session: SessionDep):
    service = MatchingService(session, get_llm(request))
    posting = _or_404(service.get_posting, posting_id)
    analysis = next((f for f in posting.fits if f.id == fit_id), None)
    if analysis is None:
        raise HTTPException(404, "Not found")
    try:
        service.advise(analysis)
        session.commit()
    except LLMUnavailable as exc:
        session.rollback()
        return _posting_page(request, session, posting, error=str(exc), status_code=503)
    except (LLMError, ValueError) as exc:
        session.rollback()
        return _posting_page(request, session, posting, error=str(exc), status_code=502)
    return redirect(f"/postings/{posting_id}#fit-{fit_id}")


# -- drafting outreach ------------------------------------------------------------------------


@router.post("/applications/{application_id}/draft")
def draft_message(
    request: Request,
    application_id: int,
    session: SessionDep,
    kind: Annotated[str, Form()] = "follow_up",
    language: Annotated[str, Form()] = "en",
    recipient: Annotated[str, Form()] = "",
    cv_version_id: Annotated[str, Form()] = "",
):
    tracker = TrackerService(session)
    application = get_application(tracker, application_id)
    try:
        cv_text = CVService(session).get(int(cv_version_id)).text if cv_version_id else None
        draft = MessageDrafter(get_llm(request)).draft(
            application, FollowupKind(kind), language=language, recipient=recipient, cv_text=cv_text
        )
    except LLMUnavailable as exc:
        return _detail(request, tracker, application, error=str(exc), status_code=503)
    except LLMError as exc:
        return _detail(request, tracker, application, error=str(exc), status_code=502)
    except (ValueError, NotFound) as exc:
        return _detail(request, tracker, application, error=str(exc), status_code=400)
    return _detail(request, tracker, application, draft=draft)
