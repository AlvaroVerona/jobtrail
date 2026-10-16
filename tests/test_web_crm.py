from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from jobtrail.database.models import Contact, Interaction, Reminder, utcnow
from jobtrail.database.session import make_engine
from jobtrail.domain.status import Status
from jobtrail.llm.fake import FakeLLM
from jobtrail.main import create_app
from jobtrail.services.contacts import ContactService, ReminderService, local_today
from jobtrail.services.tracker import TrackerService


@pytest.fixture
def engine():
    return make_engine("sqlite://")


@pytest.fixture
def client(engine):
    return TestClient(create_app(engine=engine, llm_factory=lambda: FakeLLM()))


def add_contact(engine, company="Northwind Labs", name="Sam Rivera", **kwargs) -> int:
    with Session(engine) as session:
        contact = ContactService(session).create(company, name, **kwargs)
        session.commit()
        return contact.id


def add_application(engine, company="Northwind Labs", status=Status.APPLIED, days_ago=20) -> int:
    with Session(engine) as session:
        when = utcnow() - timedelta(days=days_ago)
        app = TrackerService(session).create_application(
            company, "Data Scientist", status=status, applied_at=when, at=when
        )
        session.commit()
        return app.id


def log_sent(engine, contact_id, days_ago=5, channel="email"):
    with Session(engine) as session:
        service = ContactService(session)
        service.add_interaction(
            service.get(contact_id),
            "message",
            direction="outbound",
            channel=channel,
            at=utcnow() - timedelta(days=days_ago),
        )
        session.commit()


# -- pages ----------------------------------------------------------------------------------


def test_pages_render_empty_and_populated(client, engine):
    for page in ["/contacts", "/contacts/new", "/followups", "/analytics", "/api/reminders"]:
        assert client.get(page).status_code == 200, page
    contact_id = add_contact(engine)
    application_id = add_application(engine)
    log_sent(engine, contact_id)
    for page in [
        "/contacts",
        "/contacts?awaiting=1&q=sam",
        f"/contacts/{contact_id}",
        f"/applications/{application_id}",
        "/followups",
        "/analytics",
    ]:
        assert client.get(page).status_code == 200, page


def test_unknown_contacts_and_reminders_are_404s(client):
    assert client.get("/contacts/99").status_code == 404
    assert client.post("/contacts/99/delete").status_code == 404
    assert client.post("/contacts/99/replied").status_code == 404
    assert client.post("/reminders/99/done").status_code == 404


def test_the_new_contact_form_can_be_prefilled(client):
    page = client.get(
        "/contacts/new?company=Acme&name=Sam+Rivera&email=sam%40acme.example&application_id=4"
    ).text
    assert 'value="Acme"' in page and 'value="Sam Rivera"' in page
    assert 'value="sam@acme.example"' in page and 'name="application_id" value="4"' in page


# -- creating and editing ---------------------------------------------------------------------


def test_create_a_contact(client, engine):
    response = client.post(
        "/contacts",
        data={
            "company": "Northwind Labs",
            "name": "Sam Rivera",
            "job_title": "Talent Acquisition",
            "email": "SAM@northwind.example",
            "source": "search",
            "profile_url": "https://www.linkedin.com/in/sam",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303 and response.headers["location"].startswith("/contacts/")
    page = client.get(response.headers["location"]).text
    assert "Sam Rivera" in page and "Talent Acquisition" in page and "sam@northwind.example" in page
    assert "not contacted yet" in page


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"company": "Acme", "name": " "}, "needs a name"),
        ({"company": "Acme", "name": "Sam", "email": "nope"}, "does not look like an email"),
        ({"company": "Acme", "name": "Sam", "profile_url": "javascript:alert(1)"}, "http"),
        ({"company": "Acme", "name": "Sam", "source": "telepathy"}, "telepathy"),
        ({"company": " ", "name": "Sam"}, "empty"),
    ],
)
def test_bad_contacts_are_explained_and_the_form_keeps_what_was_typed(
    client, engine, data, message
):
    response = client.post("/contacts", data={**data, "notes": "keep me"})
    assert response.status_code == 400 and message in response.text
    assert "keep me" in response.text
    with Session(engine) as session:
        assert session.scalars(select(Contact)).all() == []


def test_the_same_person_twice_is_refused(client, engine):
    add_contact(engine, name="Sam Rivera")
    response = client.post("/contacts", data={"company": "northwind labs", "name": "sam rivera"})
    assert response.status_code == 400 and "already a contact" in response.text


def test_edit_and_delete_a_contact(client, engine):
    contact_id = add_contact(engine)
    client.post(
        f"/contacts/{contact_id}/edit",
        data={
            "name": "Samuel Rivera",
            "job_title": "HR Manager",
            "source": "referral",
            "notes": "Intro by Alex",
        },
    )
    page = client.get(f"/contacts/{contact_id}").text
    assert "Samuel Rivera" in page and "HR Manager" in page and "Intro by Alex" in page

    bad = client.post(f"/contacts/{contact_id}/edit", data={"name": "Samuel", "email": "nope"})
    assert bad.status_code == 400

    client.post(f"/contacts/{contact_id}/delete")
    assert client.get(f"/contacts/{contact_id}").status_code == 404


# -- what was said, and "they replied" ----------------------------------------------------------


def test_logging_a_message_makes_you_wait_and_replied_ends_it(client, engine):
    contact_id = add_contact(engine)
    client.post(
        f"/contacts/{contact_id}/interactions",
        data={
            "kind": "message",
            "direction": "outbound",
            "channel": "linkedin",
            "summary": "Hello Sam",
        },
    )
    page = client.get(f"/contacts/{contact_id}").text
    assert "waiting 0d" in page and "They replied" in page and "Hello Sam" in page
    assert "waiting" in client.get("/contacts?awaiting=1").text

    response = client.post(f"/contacts/{contact_id}/replied", follow_redirects=False)
    assert response.status_code == 303
    page = client.get(f"/contacts/{contact_id}").text
    assert "replied" in page and "They replied" not in page
    assert "Sam Rivera" not in client.get("/contacts?awaiting=1").text.split("<tbody>")[1]


def test_cannot_mark_replied_before_writing(client, engine):
    contact_id = add_contact(engine)
    response = client.post(f"/contacts/{contact_id}/replied")
    assert response.status_code == 400 and "nothing to answer" in response.text


@pytest.mark.parametrize(
    "data",
    [
        {"kind": "message", "direction": ""},  # a message needs a direction
        {"kind": "call", "direction": "inbound"},  # a call has none
        {"kind": "message", "direction": "outbound", "channel": "telegram"},
        {"kind": "message", "direction": "outbound", "application_id": "999"},
        {"kind": "note", "date": "19/05/2026"},
    ],
)
def test_bad_interactions_are_refused(client, engine, data):
    contact_id = add_contact(engine)
    response = client.post(f"/contacts/{contact_id}/interactions", data=data)
    assert response.status_code == 400
    with Session(engine) as session:
        assert session.scalars(select(Interaction)).all() == []


def test_an_interaction_can_be_linked_to_an_application_and_a_past_date(client, engine):
    contact_id = add_contact(engine)
    application_id = add_application(engine)
    client.post(
        f"/contacts/{contact_id}/interactions",
        data={
            "kind": "call",
            "direction": "",
            "channel": "phone",
            "date": "2026-05-19",
            "application_id": str(application_id),
            "summary": "Intro call",
        },
    )
    page = client.get(f"/contacts/{contact_id}").text
    assert (
        "19 May 2026" in page and "Intro call" in page and f"/applications/{application_id}" in page
    )


def test_an_application_of_another_company_cannot_be_linked(client, engine):
    contact_id = add_contact(engine, company="Acme")
    other = add_application(engine, company="Globex")
    response = client.post(
        f"/contacts/{contact_id}/interactions",
        data={"kind": "note", "application_id": str(other)},
    )
    assert response.status_code == 400 and "different company" in response.text


# -- messages sent from an application show up on the person -----------------------------------


def test_a_followup_to_a_contact_appears_in_their_history(client, engine):
    contact_id = add_contact(engine)
    application_id = add_application(engine)
    client.post(
        f"/applications/{application_id}/followup",
        data={
            "kind": "follow_up",
            "text": "Hello Sam, any news?",
            "contact_id": str(contact_id),
            "channel": "linkedin",
        },
    )
    page = client.get(f"/contacts/{contact_id}").text
    assert "Hello Sam, any news?" in page and "LinkedIn" in page and "waiting" in page


def test_a_followup_to_the_wrong_companys_contact_changes_nothing(client, engine):
    stranger = add_contact(engine, company="Globex", name="Alex")
    application_id = add_application(engine)
    response = client.post(
        f"/applications/{application_id}/followup",
        data={"kind": "follow_up", "contact_id": str(stranger)},
    )
    assert response.status_code == 400 and "different company" in response.text
    assert "Applied" in client.get(f"/applications/{application_id}").text  # status untouched


def test_the_application_page_lists_its_people_and_search_links(client, engine):
    add_contact(engine, name="Sam Rivera")
    application_id = add_application(engine, company="Northwind Labs")
    page = client.get(f"/applications/{application_id}").text
    assert "Sam Rivera" in page and "Find someone to write to" in page
    assert "linkedin.com/search/results/people/?keywords=Northwind+Labs" in page
    assert 'rel="noopener noreferrer"' in page and 'target="_blank"' in page
    assert "jobtrail does not run the search" in page  # the boundary is stated on the page


# -- reminders --------------------------------------------------------------------------------


def test_add_a_reminder_by_days_and_by_date(client, engine):
    application_id = add_application(engine)
    contact_id = add_contact(engine)
    client.post(
        "/reminders",
        data={"application_id": str(application_id), "days": "7", "note": "Nudge them"},
    )
    client.post("/reminders", data={"contact_id": str(contact_id), "due_on": "2030-01-15"})
    with Session(engine) as session:
        by_note = {r.note: r for r in session.scalars(select(Reminder))}
        assert by_note["Nudge them"].due_on == local_today() + timedelta(days=7)
        assert by_note[None].due_on.isoformat() == "2030-01-15"
    assert "Nudge them" in client.get(f"/applications/{application_id}").text
    assert "15 Jan" in client.get(f"/contacts/{contact_id}").text


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"days": "7"},
        {"application_id": "1", "days": "-3"},
        {"application_id": "1", "days": "abc"},
        {"application_id": "1", "due_on": "tomorrow"},
        {"application_id": "999", "days": "3"},
    ],
)
def test_bad_reminders_are_refused(client, engine, data):
    add_application(engine)
    assert client.post("/reminders", data=data).status_code == 400
    with Session(engine) as session:
        assert session.scalars(select(Reminder)).all() == []


def test_due_reminders_show_on_the_followups_page_and_can_be_finished_snoozed_or_deleted(
    client, engine
):
    application_id = add_application(engine)
    with Session(engine) as session:
        service = ReminderService(session)
        application = TrackerService(session).get_application(application_id)
        today = local_today()
        done = service.add(
            due_on=today - timedelta(days=1), application=application, note="Overdue one"
        )
        snoozed = service.add(due_on=today, application=application, note="Due today")
        deleted = service.add(due_on=today, application=application, note="To delete")
        service.add(
            due_on=today + timedelta(days=3), application=application, note="Later this week"
        )
        ids = (done.id, snoozed.id, deleted.id)
        session.commit()

    page = client.get("/followups").text
    for text in ("Overdue one", "Due today", "To delete", "Later this week"):
        assert text in page
    assert "3 due" in page

    client.post(f"/reminders/{ids[0]}/done")
    client.post(f"/reminders/{ids[1]}/snooze", data={"days": "5"})
    client.post(f"/reminders/{ids[2]}/delete")
    page = client.get("/followups").text
    assert "Overdue one" not in page and "To delete" not in page
    assert "Due today" not in page.split("Coming up")[0]  # moved out of "due"
    assert "0 due" in page


def test_reminder_redirects_stay_on_this_site(client, engine):
    application_id = add_application(engine)
    for target, expected in {
        "/followups": "/followups",
        "//evil.example": f"/applications/{application_id}",
        "https://evil.example": f"/applications/{application_id}",
    }.items():
        response = client.post(
            "/reminders",
            data={"application_id": str(application_id), "days": "3", "next": target},
            follow_redirects=False,
        )
        assert response.headers["location"] == expected, target


# -- the JSON the Mac app reads -----------------------------------------------------------------


def test_the_reminders_api_lists_only_what_is_due(client, engine):
    application_id = add_application(engine)
    contact_id = add_contact(engine)
    with Session(engine) as session:
        service = ReminderService(session)
        application = TrackerService(session).get_application(application_id)
        contact = ContactService(session).get(contact_id)
        today = local_today()
        service.add(due_on=today - timedelta(days=2), application=application)
        service.add(due_on=today, contact=contact, note="Ask about the team")
        service.add(due_on=today + timedelta(days=1), application=application, note="Tomorrow")
        finished = service.add(due_on=today, application=application, note="Finished")
        service.complete(finished)
        session.commit()

    response = client.get("/api/reminders")
    assert response.headers["content-type"].startswith("application/json")
    body = response.json()
    assert body["due"] == 2
    titles = [item["title"] for item in body["items"]]
    assert titles == ["Follow up on Northwind Labs", "Ask about the team"]
    assert body["items"][1]["url"] == f"/contacts/{contact_id}"
    assert body["items"][0]["due_on"] == (local_today() - timedelta(days=2)).isoformat()


def test_the_reminders_api_is_empty_when_nothing_is_due(client):
    assert client.get("/api/reminders").json() == {"due": 0, "items": []}


def test_the_reminders_api_does_not_answer_other_hosts(client, engine):
    add_application(engine)  # data that must not leak to a rebinding page
    assert client.get("/api/reminders", headers={"host": "evil.example"}).status_code == 400


# -- security ----------------------------------------------------------------------------------


def test_text_from_contacts_is_escaped_everywhere(client, engine):
    nasty = "<script>alert('x')</script>"
    contact_id = add_contact(engine, name=nasty, job_title=nasty, notes=nasty)
    application_id = add_application(engine)
    log_sent(engine, contact_id)
    with Session(engine) as session:
        interaction = session.scalars(select(Interaction)).one()
        interaction.summary = nasty
        ReminderService(session).add(
            due_on=local_today(), contact=session.get(Contact, contact_id), note=nasty
        )
        session.commit()
    for page in [
        "/contacts",
        f"/contacts/{contact_id}",
        f"/applications/{application_id}",
        "/followups",
    ]:
        text = client.get(page).text
        assert nasty not in text, page
        assert "&lt;script&gt;" in text, page


def test_a_stored_non_web_profile_link_is_never_rendered_as_a_link(client, engine):
    contact_id = add_contact(engine)
    with Session(engine) as session:
        session.get(Contact, contact_id).profile_url = "javascript:alert(1)"
        session.commit()
    response = client.get(f"/contacts/{contact_id}")
    assert response.status_code == 200 and "Sam Rivera" in response.text
    assert 'href="javascript' not in response.text


def test_cross_origin_writes_to_the_crm_are_blocked(client, engine):
    contact_id = add_contact(engine)
    evil = {"origin": "http://evil.example"}
    assert (
        client.post("/contacts", data={"company": "A", "name": "B"}, headers=evil).status_code
        == 403
    )
    assert client.post(f"/contacts/{contact_id}/delete", headers=evil).status_code == 403
    assert (
        client.post(
            "/reminders", data={"contact_id": str(contact_id), "days": "1"}, headers=evil
        ).status_code
        == 403
    )
    assert client.get(f"/contacts/{contact_id}").status_code == 200  # nothing was deleted


# -- analytics ---------------------------------------------------------------------------------


def test_analytics_reports_who_answered_and_flags_small_samples(client, engine):
    asked = add_contact(engine, name="Asked", source="search")
    ignored = add_contact(engine, name="Ignored", source="referral")
    log_sent(engine, asked, days_ago=8, channel="linkedin")
    log_sent(engine, ignored, days_ago=8, channel="email")
    client.post(f"/contacts/{asked}/replied")
    page = client.get("/analytics").text
    assert "Reaching people" in page and "too few to compare" in page
    assert "LinkedIn" in page and "A referral" in page
    assert "1 person(s) waiting for an answer" in page


def test_analytics_without_any_outreach_says_so(client):
    assert "You have not logged writing to anyone yet" in client.get("/analytics").text


# -- people mentioned in a posting ---------------------------------------------------------------

import json  # noqa: E402

from helpers import POSTING, posting_json  # noqa: E402
from jobtrail.llm.base import LLMUnavailable  # noqa: E402

POSTING_WITH_CONTACTS = (
    POSTING
    + "\nWrite to Talent@Northwind.example or see https://www.linkedin.com/in/sam-rivera/."
    + "\nContact Sam Rivera, Talent Acquisition Partner.\n"
)


def posting_client(engine, *answers):
    llm = FakeLLM(posting_json(), *answers)
    client = TestClient(create_app(engine=engine, llm_factory=lambda: llm))
    response = client.post(
        "/postings", data={"raw_text": POSTING_WITH_CONTACTS}, follow_redirects=False
    )
    return client, response.headers["location"]


def test_emails_and_profile_links_in_the_posting_are_listed_with_add_links(engine):
    client, posting = posting_client(engine)
    page = client.get(posting).text
    assert "People mentioned in this posting" in page
    assert "talent@northwind.example" in page and "linkedin.com/in/sam-rivera" in page
    assert (
        "/contacts/new?company=Northwind%20Labs&amp;name=&amp;job_title=&amp;"
        "email=talent%40northwind.example&amp;profile_url=&amp;source=posting"
    ) in page
    assert "profile_url=https%3A//www.linkedin.com/in/sam-rivera" in page


def test_a_posting_without_contact_details_says_so(engine):
    llm = FakeLLM(posting_json())
    client = TestClient(create_app(engine=engine, llm_factory=lambda: llm))
    location = client.post("/postings", data={"raw_text": POSTING}, follow_redirects=False).headers[
        "location"
    ]
    assert "No email addresses or LinkedIn profiles" in client.get(location).text


def test_the_model_can_suggest_names_and_nothing_is_saved(engine):
    answer = json.dumps(
        {
            "people": [
                {
                    "name": "Sam Rivera",
                    "title": "Talent Acquisition Partner",
                    "quote": "Contact Sam Rivera, Talent Acquisition Partner",
                }
            ]
        }
    )
    client, posting = posting_client(engine, answer)
    page = client.post(f"{posting}/people").text
    assert "Sam Rivera" in page and "Talent Acquisition Partner" in page
    assert "name=Sam%20Rivera&amp;job_title=Talent%20Acquisition%20Partner" in page
    with Session(engine) as session:
        assert session.scalars(select(Contact)).all() == []  # suggestions only


def test_invented_names_do_not_reach_the_page(engine):
    invented = json.dumps(
        {"people": [{"name": "Jordan Lee", "title": "CEO", "quote": "Jordan Lee, CEO"}]}
    )
    client, posting = posting_client(engine, invented, invented, invented)
    page = client.post(f"{posting}/people").text
    assert "Jordan Lee" not in page and "does not name anyone" in page


def test_name_search_with_the_model_down_is_explained(engine):
    def down(prompt):
        raise LLMUnavailable("Cannot reach Ollama at http://x. Is it running?")

    client, posting = posting_client(engine, down)
    response = client.post(f"{posting}/people")
    assert response.status_code == 503 and "Cannot reach Ollama" in response.text


def test_posting_text_with_markup_is_escaped_in_the_people_section(engine):
    nasty = "<img src=x onerror=alert(2)>"
    llm = FakeLLM(posting_json())
    client = TestClient(create_app(engine=engine, llm_factory=lambda: llm))
    text = POSTING + f"\nContact {nasty}@evil.example for details\n"
    location = client.post("/postings", data={"raw_text": text}, follow_redirects=False).headers[
        "location"
    ]
    page = client.get(location).text
    assert nasty not in page
