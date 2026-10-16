import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from helpers import CV, HONEST, POSTING, fit_json, posting_json
from jobtrail.database.models import CVVersion, FitAnalysis, Posting
from jobtrail.database.session import make_engine
from jobtrail.domain.status import Status
from jobtrail.llm.base import LLMUnavailable
from jobtrail.llm.fake import FakeLLM
from jobtrail.main import create_app
from jobtrail.services.tracker import TrackerService


@pytest.fixture
def engine():
    return make_engine("sqlite://")


def make_client(engine, llm) -> TestClient:
    return TestClient(create_app(engine=engine, llm_factory=lambda: llm))


def add_cv(engine, text=CV, label="Main CV") -> int:
    with Session(engine) as session:
        version = CVVersion(label=label, content_hash=str(hash(text + label)), text=text)
        session.add(version)
        session.commit()
        return version.id


def create_posting(client, **extra) -> str:
    response = client.post("/postings", data={"raw_text": POSTING, **extra}, follow_redirects=False)
    assert response.status_code == 303, response.text
    return response.headers["location"]


# -- CV pages ---------------------------------------------------------------------------------


def test_cv_pages_render_and_list_versions(engine):
    client = make_client(engine, FakeLLM())
    assert "No CV yet" in client.get("/cv").text
    cv_id = add_cv(engine)
    assert "Main CV" in client.get("/cv").text
    page = client.get(f"/cv/{cv_id}").text
    assert "Power BI" in page


def test_upload_a_cv_file(engine):
    client = make_client(engine, FakeLLM())
    response = client.post(
        "/cv",
        data={"label": "Uploaded"},
        files={"file": ("cv.md", CV.encode(), "text/markdown")},
    )
    assert response.status_code == 200 and "Uploaded" in response.text
    with Session(engine) as session:
        assert "Power BI" in session.scalars(select(CVVersion)).one().text


def test_pasted_cv_text_and_duplicates(engine):
    client = make_client(engine, FakeLLM())
    client.post("/cv", data={"label": "Pasted", "text": CV})
    again = client.post("/cv", data={"label": "Again", "text": CV})
    assert "already saved as" in again.text
    with Session(engine) as session:
        assert len(session.scalars(select(CVVersion)).all()) == 1


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"data": {"text": "  "}}, "empty"),
        ({"files": {"file": ("cv.exe", b"x", "application/octet-stream")}}, "Unsupported"),
        ({"files": {"file": ("cv.pdf", b"not a pdf", "application/pdf")}}, "Could not read"),
    ],
)
def test_bad_cv_input_is_explained(engine, kwargs, message):
    response = make_client(engine, FakeLLM()).post("/cv", **kwargs)
    assert response.status_code == 400 and message in response.text


def test_delete_cv_removes_its_analyses(engine):
    client = make_client(engine, FakeLLM(posting_json(), HONEST))
    cv_id = add_cv(engine)
    posting = create_posting(client)
    client.post(f"{posting}/fit", data={"cv_version_id": cv_id})
    client.post(f"/cv/{cv_id}/delete")
    with Session(engine) as session:
        assert session.scalars(select(CVVersion)).all() == []
        assert session.scalars(select(FitAnalysis)).all() == []
        assert len(session.scalars(select(Posting)).all()) == 1


def test_unknown_cv_is_a_404(engine):
    client = make_client(engine, FakeLLM())
    assert client.get("/cv/99").status_code == 404
    assert client.post("/cv/99/delete").status_code == 404


# -- postings ---------------------------------------------------------------------------------


def test_new_posting_form_warns_when_the_model_is_not_ready(engine):
    client = make_client(engine, FakeLLM(problem="Cannot reach Ollama at http://localhost:11434"))
    page = client.get("/postings/new")
    assert page.status_code == 200 and "Cannot reach Ollama" in page.text


def test_new_posting_form_remembers_the_application(engine):
    client = make_client(engine, FakeLLM())
    page = client.get("/postings/new?application_id=7").text
    assert 'name="application_id" value="7"' in page


def test_add_posting_shows_the_verified_requirements(engine):
    client = make_client(engine, FakeLLM(posting_json()))
    location = create_posting(client, url="https://northwind.example/jobs/1")
    page = client.get(location).text
    assert "Data Scientist (mid-level)" in page and "Northwind Labs" in page
    assert "Strong Python and SQL" in page  # the verbatim quote
    assert 'href="https://northwind.example/jobs/1"' in page
    assert "Northwind Labs" in client.get("/postings").text


def test_posting_errors_keep_what_the_user_typed(engine):
    client = make_client(engine, FakeLLM())
    bad_url = client.post("/postings", data={"raw_text": POSTING, "url": "javascript:alert(1)"})
    assert bad_url.status_code == 400 and "http" in bad_url.text and "Strong Python" in bad_url.text
    empty = client.post("/postings", data={"raw_text": "  "})
    assert empty.status_code == 400 and "empty" in empty.text
    unknown = client.post("/postings", data={"raw_text": POSTING, "application_id": "99"})
    assert unknown.status_code == 400 and "does not exist" in unknown.text
    with Session(engine) as session:
        assert session.scalars(select(Posting)).all() == []


def raising(error):
    def _raise(prompt):
        raise error

    return _raise


def test_model_unavailable_is_reported_and_nothing_is_stored(engine):
    llm = FakeLLM(raising(LLMUnavailable("Cannot reach Ollama at http://x. Is it running?")))
    response = make_client(engine, llm).post("/postings", data={"raw_text": POSTING})
    assert response.status_code == 503 and "Cannot reach Ollama" in response.text
    with Session(engine) as session:
        assert session.scalars(select(Posting)).all() == []


def test_unusable_model_output_is_reported(engine):
    bad = posting_json([("x", "must", "Not in the posting")])
    response = make_client(engine, FakeLLM(bad, bad, bad)).post(
        "/postings", data={"raw_text": POSTING}
    )
    assert response.status_code == 502 and "failed after 3 attempts" in response.text


def test_unknown_posting_is_a_404(engine):
    client = make_client(engine, FakeLLM())
    assert client.get("/postings/99").status_code == 404
    assert client.post("/postings/99/delete").status_code == 404


# -- fit --------------------------------------------------------------------------------------


def test_fit_page_shows_verdicts_scores_and_quotes(engine):
    client = make_client(engine, FakeLLM(posting_json(), HONEST))
    cv_id = add_cv(engine)
    posting = create_posting(client)
    response = client.post(f"{posting}/fit", data={"cv_version_id": cv_id}, follow_redirects=False)
    assert response.status_code == 303

    page = client.get(posting).text
    assert "50<small>/100</small>" in page
    assert "Automated monthly reporting with Python and SQL" in page  # evidence from the CV
    assert 'class="badge v-met"' in page and 'class="badge v-missing"' in page
    assert "Main CV" in page
    assert "50</strong>/100" in client.get("/postings").text


def test_downgraded_claims_are_visible_to_the_user(engine):
    invented = fit_json(
        (1, "met", "Led a team of twelve data scientists"),
        *[(i, "missing", None) for i in range(2, 7)],
    )
    # Same wrong answer on every attempt, so it ends up downgraded instead of fixed.
    client = make_client(engine, FakeLLM(posting_json(), invented, invented, invented))
    cv_id = add_cv(engine)
    posting = create_posting(client)
    client.post(f"{posting}/fit", data={"cv_version_id": cv_id})
    page = client.get(posting).text
    assert "1 claim(s) were downgraded" in page
    assert "but its quote is not in your CV" in page
    assert "Led a team of twelve" not in page  # the invented evidence is never shown


def test_fit_needs_a_cv(engine):
    client = make_client(engine, FakeLLM(posting_json()))
    page = client.get(create_posting(client)).text
    assert "Add a CV first" in page


def test_fit_with_the_model_down_stores_nothing(engine):
    llm = FakeLLM(posting_json(), raising(LLMUnavailable("Model 'qwen3:8b' is not installed.")))
    client = make_client(engine, llm)
    cv_id = add_cv(engine)
    posting = create_posting(client)
    response = client.post(f"{posting}/fit", data={"cv_version_id": cv_id})
    assert response.status_code == 503 and "not installed" in response.text
    with Session(engine) as session:
        assert session.scalars(select(FitAnalysis)).all() == []


def test_fit_with_an_unknown_cv_is_a_404(engine):
    client = make_client(engine, FakeLLM(posting_json()))
    posting = create_posting(client)
    assert client.post(f"{posting}/fit", data={"cv_version_id": 99}).status_code == 404


# -- links with applications ------------------------------------------------------------------


def test_posting_appears_on_its_application_and_survives_its_deletion(engine):
    client = make_client(engine, FakeLLM(posting_json()))  # creates the tables
    with Session(engine) as session:
        application_id = (
            TrackerService(session).create_application("Northwind Labs", status=Status.APPLIED).id
        )
        session.commit()
    posting = create_posting(client, application_id=str(application_id))

    page = client.get(f"/applications/{application_id}").text
    assert posting in page and "Data Scientist (mid-level)" in page
    assert f"/postings/new?application_id={application_id}" in page

    client.post(f"/applications/{application_id}/delete")
    assert client.get(posting).status_code == 200


# -- security ---------------------------------------------------------------------------------


def test_text_from_postings_cvs_and_the_model_is_escaped(engine):
    nasty = "<script>alert(1)</script>"
    posting_text = f"{nasty} Engineer at Evil Corp\n- Strong Python and SQL\n"
    answer = posting_json(
        [(nasty, "must", "Strong Python and SQL")], title=nasty, company="Evil Corp"
    )
    cv = f"Skills: Python and SQL. {nasty}"
    reasoned = fit_json((1, "met", "Skills: Python and SQL"))
    reasoned = reasoned.replace("because", nasty)
    client = make_client(engine, FakeLLM(answer, reasoned))
    cv_id = add_cv(engine, text=cv, label=nasty)
    posting = client.post("/postings", data={"raw_text": posting_text}, follow_redirects=False)
    location = posting.headers["location"]
    client.post(f"{location}/fit", data={"cv_version_id": cv_id})

    for page in (location, "/postings", f"/cv/{cv_id}", "/cv"):
        text = client.get(page).text
        assert nasty not in text, page
        assert "&lt;script&gt;" in text, page


def test_a_stored_non_web_url_is_never_rendered_as_a_link(engine):
    client = make_client(engine, FakeLLM(posting_json()))
    location = create_posting(client)
    with Session(engine) as session:
        session.scalars(select(Posting)).one().url = "javascript:alert(1)"
        session.commit()
    assert "javascript:" not in client.get(location).text


def test_cross_origin_uploads_and_analyses_are_blocked(engine):
    client = make_client(engine, FakeLLM(posting_json()))
    evil = {"origin": "http://evil.example"}
    assert client.post("/cv", data={"text": CV}, headers=evil).status_code == 403
    assert client.post("/postings", data={"raw_text": POSTING}, headers=evil).status_code == 403
    with Session(engine) as session:
        assert session.scalars(select(CVVersion)).all() == []


def test_the_model_client_is_not_created_for_pages_that_do_not_need_it(engine):
    def forbidden():
        raise AssertionError("the model client must not be built for this page")

    client = TestClient(create_app(engine=engine, llm_factory=forbidden))
    for page in ["/board", "/applications", "/followups", "/analytics", "/settings", "/cv"]:
        assert client.get(page).status_code == 200, page
