import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from helpers import CV, HONEST, POSTING, posting_json
from jobtrail.database.models import CVVersion, FitAnalysis, Followup, utcnow
from jobtrail.database.session import make_engine
from jobtrail.domain.status import Status
from jobtrail.llm.base import LLMUnavailable
from jobtrail.llm.fake import FakeLLM
from jobtrail.main import create_app
from jobtrail.services.tracker import TrackerService
from test_outreach import GOOD, GOOD_ADVICE, advice_json, answer


@pytest.fixture
def engine():
    return make_engine("sqlite://")


def make_client(engine, llm=None) -> TestClient:
    return TestClient(create_app(engine=engine, llm_factory=lambda: llm or FakeLLM()))


def seed_application(engine, status=Status.APPLIED, company="Northwind Labs", days_ago=20) -> int:
    with Session(engine) as session:
        when = utcnow() - timedelta(days=days_ago)
        app = TrackerService(session).create_application(
            company, "Data Scientist", status=status, applied_at=when, at=when
        )
        session.commit()
        return app.id


def add_cv(engine) -> int:
    with Session(engine) as session:
        version = CVVersion(label="Main CV", content_hash="h1", text=CV)
        session.add(version)
        session.commit()
        return version.id


# -- recording and viewing messages -----------------------------------------------------------


def test_application_page_shows_the_messages_section(engine):
    client = make_client(engine)
    application_id = seed_application(engine)
    page = client.get(f"/applications/{application_id}").text
    assert 'id="outreach"' in page and "Nothing sent yet" in page
    assert "Draft a message" in page and "Log one I already sent" in page


def test_logging_a_message_without_text(engine):
    client = make_client(engine)
    application_id = seed_application(engine)
    response = client.post(
        f"/applications/{application_id}/followup",
        data={"kind": "follow_up"},
        follow_redirects=False,
    )
    assert response.headers["location"] == f"/applications/{application_id}#outreach"
    page = client.get(f"/applications/{application_id}").text
    assert "Follow-up" in page and "Follow-up sent" in page  # the log entry and the new status
    assert "Nothing sent yet" not in page


def test_recording_a_draft_stores_subject_and_text(engine):
    client = make_client(engine)
    application_id = seed_application(engine)
    client.post(
        f"/applications/{application_id}/followup",
        data={"kind": "follow_up", "subject": "Checking in", "text": "Hello, any news?"},
    )
    with Session(engine) as session:
        assert (
            session.scalars(select(Followup)).one().text
            == "Subject: Checking in\n\nHello, any news?"
        )
    assert "Hello, any news?" in client.get(f"/applications/{application_id}").text


def test_a_message_that_does_not_fit_the_status_is_refused(engine):
    client = make_client(engine)
    application_id = seed_application(engine, status=Status.WISHLIST)
    response = client.post(f"/applications/{application_id}/followup", data={"kind": "thank_you"})
    assert response.status_code == 400 and "does not fit" in response.text
    bad_kind = client.post(
        f"/applications/{application_id}/followup", data={"kind": "carrier-pigeon"}
    )
    assert bad_kind.status_code == 400
    with Session(engine) as session:
        assert session.scalars(select(Followup)).all() == []


def test_only_fitting_kinds_are_offered(engine):
    client = make_client(engine)
    applied = seed_application(engine)
    page = client.get(f"/applications/{applied}").text
    assert '<option value="follow_up">' in page and '<option value="thank_you">' not in page
    interviewing = seed_application(engine, status=Status.INTERVIEWING, company="Other")
    assert '<option value="thank_you">' in client.get(f"/applications/{interviewing}").text


# -- drafting ---------------------------------------------------------------------------------


def draft_request(client, application_id, **extra):
    data = {"kind": "follow_up", "language": "en", "recipient": "Sam", **extra}
    return client.post(f"/applications/{application_id}/draft", data=data)


def test_drafting_shows_an_editable_message(engine):
    client = make_client(engine, FakeLLM(GOOD))
    application_id = seed_application(engine)
    cv_id = add_cv(engine)
    response = draft_request(client, application_id, cv_version_id=str(cv_id))
    assert response.status_code == 200
    assert "Hello Sam, I applied 20 days ago" in response.text
    assert "<textarea" in response.text and "I sent it: record it" in response.text
    assert "built dashboards in Power BI used by 40 finance users" in response.text  # the body
    # The model's list of "words it relied on" is not shown: nothing proves the text uses it.
    assert "relies on these words" not in response.text
    with Session(engine) as session:
        assert session.scalars(select(Followup)).all() == []  # a draft is not a sent message


def test_drafting_without_a_cv_makes_no_claims_about_experience(engine):
    llm = FakeLLM(answer("Hello, I applied 20 days ago and would like an update."))
    client = make_client(engine, llm)
    draft_request(client, seed_application(engine))
    assert "say nothing about the candidate's experience" in llm.prompts[0]


def test_unsupported_numbers_show_a_warning_to_the_user(engine):
    bad = answer("Hello, I raised revenue by 300% at my last job.")
    client = make_client(engine, FakeLLM(bad, bad, bad))
    response = draft_request(client, seed_application(engine))
    assert response.status_code == 200
    assert "numbers that are not in your CV" in response.text and "300" in response.text


def test_drafting_with_the_model_down_explains_it(engine):
    def down(prompt):
        raise LLMUnavailable("Cannot reach Ollama at http://x. Is it running?")

    client = make_client(engine, FakeLLM(down))
    response = draft_request(client, seed_application(engine))
    assert response.status_code == 503 and "Cannot reach Ollama" in response.text


def test_drafting_errors_are_reported(engine):
    client = make_client(engine, FakeLLM("garbage", "garbage", "garbage"))
    application_id = seed_application(engine)
    assert draft_request(client, application_id).status_code == 502
    assert draft_request(client, application_id, language="fr").status_code == 400
    assert draft_request(client, application_id, cv_version_id="999").status_code == 400
    assert draft_request(client, 999).status_code == 404


def test_model_text_in_a_draft_is_escaped(engine):
    nasty = "<script>alert('x')</script>"  # no digits: those would trigger the number check
    client = make_client(
        engine, FakeLLM(answer(f"Hello {nasty} I applied 20 days ago.", subject=nasty))
    )
    response = draft_request(client, seed_application(engine))
    assert nasty not in response.text and "&lt;script&gt;" in response.text


def test_cross_origin_message_actions_are_blocked(engine):
    client = make_client(engine, FakeLLM(GOOD))
    application_id = seed_application(engine)
    evil = {"origin": "http://evil.example"}
    assert client.post(f"/applications/{application_id}/followup", headers=evil).status_code == 403
    assert client.post(f"/applications/{application_id}/draft", headers=evil).status_code == 403


# -- CV advice --------------------------------------------------------------------------------


def posting_with_fit(client, engine):
    cv_id = add_cv(engine)
    location = client.post("/postings", data={"raw_text": POSTING}, follow_redirects=False).headers[
        "location"
    ]
    client.post(f"{location}/fit", data={"cv_version_id": cv_id})
    return location


def test_advice_is_requested_and_shown_under_its_analysis(engine):
    llm = FakeLLM(posting_json(), HONEST, GOOD_ADVICE)
    client = make_client(engine, llm)
    location = posting_with_fit(client, engine)
    assert "Get CV advice" in client.get(location).text

    with Session(engine) as session:
        fit_id = session.scalars(select(FitAnalysis)).one().id
    response = client.post(f"{location}/fit/{fit_id}/advice", follow_redirects=False)
    assert response.headers["location"] == f"{location}#fit-{fit_id}"

    page = client.get(location).text
    assert "Get CV advice" not in page  # already has advice
    assert "Automated monthly reporting with Python and SQL" in page
    assert "Not evidenced in your CV" in page and "Kubernetes" in page
    assert 'class="chip">Power BI' in page  # keyword chips


def test_invented_advice_never_reaches_the_page(engine):
    invented = advice_json([(2, "Ten years of Python at Google", "experience")])
    client = make_client(engine, FakeLLM(posting_json(), HONEST, invented, invented, invented))
    location = posting_with_fit(client, engine)
    with Session(engine) as session:
        fit_id = session.scalars(select(FitAnalysis)).one().id
    client.post(f"{location}/fit/{fit_id}/advice")
    page = client.get(location).text
    assert "Ten years of Python" not in page
    assert "1 suggestion(s) were left out" in page


def test_advice_with_the_model_down_stores_nothing(engine):
    def down(prompt):
        raise LLMUnavailable("Model 'qwen3:8b' is not installed.")

    client = make_client(engine, FakeLLM(posting_json(), HONEST, down))
    location = posting_with_fit(client, engine)
    with Session(engine) as session:
        fit_id = session.scalars(select(FitAnalysis)).one().id
    response = client.post(f"{location}/fit/{fit_id}/advice")
    assert response.status_code == 503 and "not installed" in response.text
    with Session(engine) as session:
        assert "advice" not in session.scalars(select(FitAnalysis)).one().detail


def test_advice_for_an_unknown_analysis_is_a_404(engine):
    client = make_client(engine, FakeLLM(posting_json()))
    location = client.post("/postings", data={"raw_text": POSTING}, follow_redirects=False).headers[
        "location"
    ]
    assert client.post(f"{location}/fit/999/advice").status_code == 404


def test_advice_text_from_the_model_is_escaped(engine):
    nasty = "<img src=x onerror=alert(1)>"
    advice = json.dumps(
        {
            "highlights": [
                {"id": 2, "cv_quote": "Python, SQL, pandas", "placement": "skills", "why": nasty}
            ],
            "keywords": [],
        }
    )
    client = make_client(engine, FakeLLM(posting_json(), HONEST, advice))
    location = posting_with_fit(client, engine)
    with Session(engine) as session:
        fit_id = session.scalars(select(FitAnalysis)).one().id
    client.post(f"{location}/fit/{fit_id}/advice")
    page = client.get(location).text
    assert nasty not in page and "&lt;img" in page
