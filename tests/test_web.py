from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from jobtrail.config import Settings
from jobtrail.database.models import Application, Channel, Track, utcnow
from jobtrail.database.session import make_engine
from jobtrail.demo import load_demo
from jobtrail.domain.status import Status
from jobtrail.main import create_app
from jobtrail.services.tracker import TrackerService
from jobtrail.web.routes import parse_date, resolve_at, safe_next


@pytest.fixture
def engine():
    return make_engine("sqlite://")


@pytest.fixture
def client(engine):
    return TestClient(create_app(engine=engine))


@pytest.fixture
def seed(engine):
    """Create applications directly in the database; returns the new application's id."""

    def _seed(company="Acme", role="Data Scientist", **kwargs) -> int:
        with Session(engine) as session:
            app = TrackerService(session).create_application(company, role, **kwargs)
            session.commit()
            return app.id

    return _seed


def status_of(engine, application_id: int) -> Status:
    with Session(engine) as session:
        return session.get(Application, application_id).status


# -- pages render -------------------------------------------------------------------------------


def test_every_page_renders_with_demo_data(client, engine):
    with Session(engine) as session:
        load_demo(session)
    pages = [
        "/board",
        "/board?track=Data+Science/AI",
        "/applications",
        "/applications?q=analyst&status=applied&track=Data+Science/AI&channel=Referral",
        "/applications/1",
        "/applications/new",
        "/followups",
        "/analytics",
        "/settings",
    ]
    for page in pages:
        response = client.get(page)
        assert response.status_code == 200, page
        assert "jobtrail" in response.text


def test_pages_render_on_an_empty_database(client):
    for page in ["/board", "/applications", "/followups", "/analytics", "/settings"]:
        assert client.get(page).status_code == 200, page


def test_root_redirects_to_the_board(client):
    response = client.get("/", follow_redirects=False)
    assert (response.status_code, response.headers["location"]) == (303, "/board")


def test_unknown_application_is_a_friendly_404(client):
    response = client.get("/applications/999")
    assert response.status_code == 404
    assert "Page not found" in response.text
    assert client.post("/applications/999/delete", follow_redirects=False).status_code == 404


def test_static_files_are_served(client):
    assert client.get("/static/style.css").status_code == 200
    assert client.get("/static/htmx.min.js").status_code == 200


# -- creating ---------------------------------------------------------------------------------


def test_create_application_redirects_to_its_detail_page(client, engine):
    response = client.post(
        "/applications",
        data={
            "company": "Acme",
            "role": "ML Engineer",
            "status": "wishlist",
            "track": "Data Science/AI",
            "channel": "Referral",
            "salary_expected": "45",
            "notes": "via Sam",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    page = client.get(response.headers["location"])
    assert "Acme" in page.text and "ML Engineer" in page.text and "via Sam" in page.text
    with Session(engine) as session:
        app = session.scalars(select(Application)).one()
        assert (app.track.name, app.channel.name, app.salary_expected) == (
            "Data Science/AI",
            "Referral",
            45,
        )


def test_creating_an_applied_application_without_a_date_uses_today(client, engine):
    client.post("/applications", data={"company": "Acme", "status": "applied"})
    with Session(engine) as session:
        app = session.scalars(select(Application)).one()
        assert (utcnow() - app.applied_at) < timedelta(minutes=1)


def test_creating_an_applied_application_with_a_past_date(client, engine):
    client.post(
        "/applications",
        data={"company": "Acme", "status": "applied", "applied_date": "2026-05-19"},
    )
    with Session(engine) as session:
        app = session.scalars(select(Application)).one()
        assert app.applied_at == datetime(2026, 5, 19, tzinfo=UTC)
        assert app.events[0].at == app.applied_at


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"company": "  "}, "empty"),
        ({"company": "Acme", "status": "offer"}, "can only start as"),
        ({"company": "Acme", "status": "nonsense"}, "not a valid"),
        ({"company": "Acme", "status": "applied", "applied_date": "19/05/2026"}, "YYYY-MM-DD"),
        ({"company": "Acme", "salary_expected": "lots"}, "whole number"),
    ],
)
def test_invalid_creation_shows_an_error_and_keeps_the_form(client, engine, data, message):
    response = client.post("/applications", data={**data, "role": "Keep me"})
    assert response.status_code == 400
    assert message in response.text
    assert "Keep me" in response.text  # what the user typed is not lost
    with Session(engine) as session:
        assert session.scalars(select(Application)).all() == []


# -- changing status --------------------------------------------------------------------------


def test_change_status_updates_the_application_and_its_history(client, engine, seed):
    application_id = seed(status=Status.APPLIED)
    response = client.post(
        f"/applications/{application_id}/status",
        data={"to_status": "screening", "note": "Recruiter call"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert status_of(engine, application_id) is Status.SCREENING
    page = client.get(f"/applications/{application_id}").text
    assert "Recruiter call" in page and "Screening" in page


def test_invalid_transition_is_refused_and_explained(client, engine, seed):
    application_id = seed(status=Status.WISHLIST)
    response = client.post(f"/applications/{application_id}/status", data={"to_status": "offer"})
    assert response.status_code == 400
    assert "Cannot move from" in response.text
    assert status_of(engine, application_id) is Status.WISHLIST


def test_a_date_on_the_same_day_as_the_last_event_is_accepted(client, engine, seed):
    application_id = seed(status=Status.APPLIED)  # event is "now", i.e. later than midnight
    today = utcnow().strftime("%Y-%m-%d")
    response = client.post(
        f"/applications/{application_id}/status",
        data={"to_status": "screening", "date": today},
        follow_redirects=False,
    )
    assert response.status_code == 303


def test_a_date_before_the_previous_event_is_refused(client, seed):
    application_id = seed(status=Status.APPLIED)
    response = client.post(
        f"/applications/{application_id}/status",
        data={"to_status": "screening", "date": "2020-01-01"},
    )
    assert response.status_code == 400
    assert "before the previous event" in response.text


def test_final_statuses_offer_no_further_moves(client, seed):
    application_id = seed(status=Status.APPLIED)
    client.post(f"/applications/{application_id}/status", data={"to_status": "rejected"})
    assert "final status" in client.get(f"/applications/{application_id}").text


def test_next_parameter_cannot_redirect_off_site(client, seed):
    cases = {
        "/followups": "/followups",
        "//evil.example/x": "default",
        "https://evil.example": "default",
        "/\\evil.example": "default",
    }
    for target, expected in cases.items():
        application_id = seed(status=Status.WISHLIST)
        response = client.post(
            f"/applications/{application_id}/status",
            data={"to_status": "to_apply", "next": target},
            follow_redirects=False,
        )
        want = f"/applications/{application_id}" if expected == "default" else expected
        assert response.headers["location"] == want, target


def test_safe_next_unit():
    assert safe_next("/board?track=x", "/d") == "/board?track=x"
    assert safe_next("", "/d") == "/d"
    assert safe_next("http://evil.example", "/d") == "/d"


# -- editing and deleting ---------------------------------------------------------------------


def test_edit_application_details(client, engine, seed):
    application_id = seed()
    response = client.post(
        f"/applications/{application_id}/edit",
        data={
            "role": "Senior DS",
            "applied_date": "2026-06-02",
            "channel": "Recruiter",
            "track": "Data",
            "salary_expected": "55",
            "salary_asked": "",
            "notes": "updated",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    with Session(engine) as session:
        app = session.get(Application, application_id)
        assert (app.role, app.channel.name, app.track.name, app.salary_expected) == (
            "Senior DS",
            "Recruiter",
            "Data",
            55,
        )
        assert app.applied_at == datetime(2026, 6, 2, tzinfo=UTC)
        assert app.salary_asked is None


def test_edit_with_bad_input_changes_nothing(client, engine, seed):
    application_id = seed(role="Original")
    response = client.post(
        f"/applications/{application_id}/edit", data={"role": "Changed", "salary_asked": "abc"}
    )
    assert response.status_code == 400
    with Session(engine) as session:
        assert session.get(Application, application_id).role == "Original"


def test_delete_application(client, engine, seed):
    application_id = seed()
    response = client.post(f"/applications/{application_id}/delete", follow_redirects=False)
    assert (response.status_code, response.headers["location"]) == (303, "/applications")
    assert client.get(f"/applications/{application_id}").status_code == 404


# -- lists, board, follow-ups -----------------------------------------------------------------


def test_board_places_applications_in_their_columns(client, seed):
    seed("Wishlisted Co", status=Status.WISHLIST)
    seed("Interview Co", status=Status.INTERVIEWING)
    seed("Closed Co", status=Status.REJECTED)
    page = client.get("/board").text
    column = lambda title: page.split(f"<h2>{title}")[1].split("</section>")[0]  # noqa: E731
    assert "Wishlisted Co" in column("Wishlist")
    assert "Interview Co" in column("Interviewing")
    assert "Closed Co" in page.split("Closed</h2>")[1]
    assert "Closed Co" not in column("Wishlist")


def test_board_filters_by_track(client, seed):
    seed("Data Co", track="Data")
    seed("Finance Co", track="Finance")
    page = client.get("/board?track=Data").text
    assert "Data Co" in page and "Finance Co" not in page


def test_applications_list_filters(client, seed):
    seed("Alpha", "Data Scientist", status=Status.APPLIED, track="Data", channel="Referral")
    seed("Beta", "Analyst", status=Status.WISHLIST, track="Finance")
    assert "Beta" not in client.get("/applications?q=scientist").text
    assert "Alpha" not in client.get("/applications?status=wishlist").text
    assert "Alpha" not in client.get("/applications?track=Finance").text
    assert "Beta" not in client.get("/applications?channel=Referral").text
    assert "Alpha" in client.get("/applications?status=bogus").text  # bad filter is ignored


def test_search_treats_wildcards_literally(client, seed):
    seed("Alpha")
    assert "Alpha" not in client.get("/applications?q=%25").text  # '%' must not match everything


def test_followups_lists_overdue_applications_with_actions(client, engine, seed):
    old = utcnow() - timedelta(days=40)
    waiting = seed("Overdue Co", status=Status.APPLIED, at=old, applied_at=old)
    seed("Fresh Co", status=Status.APPLIED)
    chased = seed("Chased Co", status=Status.FOLLOW_UP_SENT, at=old)
    page = client.get("/followups").text
    assert "Overdue Co" in page and "Chased Co" in page and "Fresh Co" not in page
    # A follow-up can be recorded again, even for one already nudged.
    assert page.count("Follow-up sent</button>") == 2

    response = client.post(
        f"/applications/{waiting}/followup",
        data={"next": "/followups"},
        follow_redirects=False,
    )
    assert response.headers["location"] == "/followups"
    assert status_of(engine, waiting) is Status.FOLLOW_UP_SENT

    client.post(f"/applications/{chased}/followup", data={"next": "/followups"})
    assert status_of(engine, chased) is Status.FOLLOW_UP_SENT  # status unchanged...
    page = client.get("/followups").text
    assert "Overdue Co" not in page and "Chased Co" not in page  # ...but the clock restarted


def test_analytics_page_warns_about_small_samples(client, seed):
    seed("Acme", status=Status.APPLIED, channel="Referral")
    page = client.get("/analytics").text
    assert "too few to compare" in page
    assert "Referral" in page


# -- settings ---------------------------------------------------------------------------------


def test_add_rename_and_delete_a_track(client, engine):
    client.post("/settings/tracks", data={"name": "Quant"})
    with Session(engine) as session:
        track = session.scalars(select(Track)).one()
    assert "Quant" in client.get("/settings").text

    client.post(f"/settings/tracks/{track.id}/rename", data={"name": "Quantitative"})
    assert "Quantitative" in client.get("/settings").text

    client.post(f"/settings/tracks/{track.id}/delete")
    with Session(engine) as session:
        assert session.scalars(select(Track)).all() == []


def test_option_in_use_cannot_be_deleted(client, engine, seed):
    seed(track="Data")
    with Session(engine) as session:
        track = session.scalars(select(Track)).one()
    response = client.post(f"/settings/tracks/{track.id}/delete")
    assert response.status_code == 400
    assert "used by 1 application" in response.text
    with Session(engine) as session:
        assert session.scalars(select(Track)).one().name == "Data"


def test_rename_to_an_existing_name_is_refused(client, engine):
    with Session(engine) as session:
        recruiter = session.scalars(select(Channel).where(Channel.name == "Recruiter")).one()
    response = client.post(f"/settings/channels/{recruiter.id}/rename", data={"name": "referral"})
    assert response.status_code == 400
    assert "already exists" in response.text


def test_empty_option_names_and_unknown_kinds(client):
    assert client.post("/settings/tracks", data={"name": " "}).status_code == 400
    assert client.post("/settings/bogus", data={"name": "x"}).status_code == 404


# -- security ---------------------------------------------------------------------------------


def test_html_in_user_data_is_escaped(client, seed):
    seed("<script>alert(1)</script>", "<img src=x onerror=alert(2)>", notes="<b>bold</b>")
    for page in ["/board", "/applications", "/applications/1"]:
        text = client.get(page).text
        assert "<script>alert(1)</script>" not in text, page
        assert "<img src=x" not in text, page
        assert "&lt;script&gt;" in text, page


def test_cross_origin_writes_are_blocked(client, engine, seed):
    application_id = seed()
    for headers in ({"origin": "http://evil.example"}, {"sec-fetch-site": "cross-site"}):
        response = client.post(f"/applications/{application_id}/delete", headers=headers)
        assert response.status_code == 403, headers
    assert client.get(f"/applications/{application_id}").status_code == 200  # still there


def test_same_origin_writes_are_allowed(client, seed):
    application_id = seed()
    response = client.post(
        f"/applications/{application_id}/delete",
        headers={"origin": "http://testserver", "sec-fetch-site": "same-origin"},
        follow_redirects=False,
    )
    assert response.status_code == 303


def test_reads_do_not_depend_on_origin(client):
    assert client.get("/board", headers={"origin": "http://evil.example"}).status_code == 200


def test_unknown_host_header_is_rejected(client):
    # DNS rebinding: a hostile site resolving to 127.0.0.1 still sends its own Host header.
    assert client.get("/board", headers={"host": "evil.example"}).status_code == 400


# -- helpers and wiring -----------------------------------------------------------------------


def test_resolve_at_rules():
    now = datetime(2026, 6, 10, 15, 0, tzinfo=UTC)
    last = datetime(2026, 6, 8, 14, 30, tzinfo=UTC)
    midnight = lambda d: datetime(2026, 6, d, tzinfo=UTC)  # noqa: E731
    assert resolve_at(None, last, now) is None
    assert resolve_at(midnight(10), last, now) == now  # today means now
    assert resolve_at(midnight(8), last, now) == last  # same day as last event: not before it
    assert resolve_at(midnight(9), last, now) == midnight(9)


def test_parse_date():
    assert parse_date("") is None
    assert parse_date("2026-05-19") == datetime(2026, 5, 19, tzinfo=UTC)
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        parse_date("May 19")


def test_create_app_builds_the_configured_database(tmp_path):
    settings = Settings(data_dir=tmp_path / "nested", db_file="x.db")
    TestClient(create_app(settings=settings))
    assert (tmp_path / "nested" / "x.db").is_file()
