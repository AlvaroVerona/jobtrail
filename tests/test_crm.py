from datetime import UTC, date, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from sqlalchemy import select

from helpers import POSTING
from jobtrail.database.models import Contact, Followup, Interaction, Reminder
from jobtrail.domain.followup import FollowupKind
from jobtrail.domain.outreach import thread_state
from jobtrail.domain.status import Status
from jobtrail.llm.base import LLMError
from jobtrail.llm.fake import FakeLLM
from jobtrail.services.contact_extractor import ContactExtractor
from jobtrail.services.contacts import (
    ContactService,
    ReminderService,
    find_mentions,
    reminder_title,
    reminder_url,
    search_links,
)
from jobtrail.services.outreach_stats import OutreachStatsService

NOW = datetime(2026, 7, 1, 10, 0, tzinfo=UTC)


def day(offset: int) -> datetime:
    return NOW + timedelta(days=offset)


class FakeInteraction:
    def __init__(self, kind, direction, offset):
        self.kind, self.direction, self.at = kind, direction, day(offset)


def msg(direction, offset):
    return FakeInteraction("message", direction, offset)


# -- the rule that decides "did they answer?" ---------------------------------------------------


def test_nothing_logged_means_nothing_to_wait_for():
    state = thread_state([], NOW)
    assert (state.contacted, state.responded, state.awaiting) == (False, False, False)


def test_a_message_from_you_that_nobody_answered_is_awaiting():
    state = thread_state([msg("outbound", -5)], NOW)
    assert (state.contacted, state.responded, state.awaiting, state.days_waiting) == (
        True,
        False,
        True,
        5,
    )


def test_an_answer_ends_the_wait_and_gives_the_response_time():
    state = thread_state([msg("outbound", -10), msg("inbound", -7)], NOW)
    assert (state.responded, state.awaiting, state.days_waiting) == (True, False, None)
    assert state.response_days == pytest.approx(3)


def test_writing_again_after_their_answer_means_waiting_again():
    state = thread_state([msg("outbound", -10), msg("inbound", -8), msg("outbound", -2)], NOW)
    assert state.responded and state.awaiting and state.days_waiting == 2
    assert state.response_days == pytest.approx(2)  # measured from the first message


def test_a_message_from_them_before_you_wrote_is_not_an_answer():
    state = thread_state([msg("inbound", -9), msg("outbound", -5)], NOW)
    assert state.contacted and not state.responded and state.awaiting


def test_a_call_or_meeting_counts_as_them_responding():
    for kind in ("call", "meeting"):
        state = thread_state([msg("outbound", -6), FakeInteraction(kind, None, -3)], NOW)
        assert state.responded and not state.awaiting


def test_notes_never_count_as_an_answer_or_a_message():
    state = thread_state([msg("outbound", -6), FakeInteraction("note", None, -1)], NOW)
    assert not state.responded and state.awaiting and state.days_waiting == 6
    assert state.last_activity == day(-1)  # but they are activity


def test_a_contact_you_only_have_notes_about_was_never_contacted():
    state = thread_state([FakeInteraction("note", None, -1)], NOW)
    assert not state.contacted and not state.awaiting


def test_order_in_the_list_does_not_matter():
    shuffled = [msg("inbound", -7), msg("outbound", -10)]
    assert thread_state(shuffled, NOW).responded


# -- contacts -----------------------------------------------------------------------------------


@pytest.fixture
def contacts(session):
    return ContactService(session)


def test_create_a_contact_and_its_company(contacts, session):
    contact = contacts.create(
        "Northwind Labs",
        "  Sam Rivera ",
        job_title="Talent Acquisition",
        profile_url="https://www.linkedin.com/in/sam-rivera",
        email="Sam@Northwind.example",
        source="search",
        notes="Met via LinkedIn",
    )
    assert (contact.name, contact.email, contact.source) == (
        "Sam Rivera",
        "sam@northwind.example",
        "search",
    )
    assert contact.company.name == "Northwind Labs"


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"name": "  "}, "needs a name"),
        ({"name": "Sam", "email": "not-an-email"}, "does not look like an email"),
        ({"name": "Sam", "profile_url": "javascript:alert(1)"}, "http"),
        ({"name": "Sam", "source": "telepathy"}, "telepathy"),
    ],
)
def test_bad_contact_input_is_refused(contacts, session, kwargs, message):
    with pytest.raises(ValueError, match=message):
        contacts.create("Acme", **kwargs)
    assert session.scalars(select(Contact)).all() == []


def test_the_same_person_cannot_be_added_twice_at_a_company(contacts):
    contacts.create("Acme", "Sam Rivera")
    with pytest.raises(ValueError, match="already a contact"):
        contacts.create("acme", "sam rivera")
    contacts.create("Globex", "Sam Rivera")  # a different company is fine


def test_update_validates_like_create(contacts):
    sam = contacts.create("Acme", "Sam")
    contacts.create("Acme", "Alex")
    contacts.update(sam, job_title="  HR  ", email="s@acme.example", notes="")
    assert (sam.job_title, sam.email, sam.notes) == ("HR", "s@acme.example", None)
    with pytest.raises(ValueError, match="already a contact"):
        contacts.update(sam, name="alex")
    with pytest.raises(ValueError, match="Cannot edit"):
        contacts.update(sam, company_id=3)


def test_deleting_a_contact_removes_its_history_but_not_the_application(contacts, tracker, session):
    application = tracker.create_application("Acme", status=Status.APPLIED, at=day(-30))
    sam = contacts.create("Acme", "Sam")
    contacts.add_interaction(
        sam, "message", direction="outbound", application=application, at=day(-3)
    )
    contacts.delete(sam)
    session.commit()
    assert session.scalars(select(Interaction)).all() == []
    assert tracker.list_applications() == [application]


# -- interactions and "replied" ----------------------------------------------------------------


def test_a_message_needs_a_direction_and_a_call_must_not_have_one(contacts):
    sam = contacts.create("Acme", "Sam")
    with pytest.raises(ValueError, match="needs a direction"):
        contacts.add_interaction(sam, "message")
    with pytest.raises(ValueError, match="no direction"):
        contacts.add_interaction(sam, "call", direction="inbound")
    with pytest.raises(ValueError):
        contacts.add_interaction(sam, "carrier-pigeon")
    with pytest.raises(ValueError):
        contacts.add_interaction(sam, "message", direction="outbound", channel="telegram")


def test_naive_times_are_refused(contacts):
    sam = contacts.create("Acme", "Sam")
    with pytest.raises(ValueError, match="timezone-aware"):
        contacts.add_interaction(sam, "note", at=datetime(2026, 7, 1))


def test_mark_replied_logs_an_incoming_message_on_the_same_channel(contacts):
    sam = contacts.create("Acme", "Sam")
    contacts.add_interaction(sam, "message", direction="outbound", channel="linkedin", at=day(-4))
    reply = contacts.mark_replied(sam, at=day(-1))
    assert (reply.direction, reply.channel, reply.kind) == ("inbound", "linkedin", "message")
    state = contacts.state(sam, now=NOW)
    assert state.responded and not state.awaiting and state.response_days == pytest.approx(3)


def test_cannot_mark_replied_before_writing(contacts):
    sam = contacts.create("Acme", "Sam")
    with pytest.raises(ValueError, match="nothing to answer"):
        contacts.mark_replied(sam)


def test_an_interaction_cannot_point_at_another_companys_application(contacts, tracker):
    application = tracker.create_application("Globex")
    sam = contacts.create("Acme", "Sam")
    with pytest.raises(ValueError, match="different company"):
        contacts.add_interaction(sam, "note", application=application)


def test_the_list_puts_the_people_you_are_waiting_on_first(contacts):
    quiet = contacts.create("Acme", "Quiet")
    waiting_long = contacts.create("Acme", "Waiting long")
    waiting_short = contacts.create("Globex", "Waiting short")
    answered = contacts.create("Globex", "Answered")
    contacts.add_interaction(waiting_long, "message", direction="outbound", at=day(-20))
    contacts.add_interaction(waiting_short, "message", direction="outbound", at=day(-2))
    contacts.add_interaction(answered, "message", direction="outbound", at=day(-9))
    contacts.add_interaction(answered, "message", direction="inbound", at=day(-8))

    names = [r.contact.name for r in contacts.list_contacts(now=NOW)]
    assert names[:2] == ["Waiting long", "Waiting short"]
    assert [r.contact.name for r in contacts.list_contacts(awaiting_only=True, now=NOW)] == [
        "Waiting long",
        "Waiting short",
    ]
    assert {r.contact.name for r in contacts.list_contacts(search="globex", now=NOW)} == {
        "Answered",
        "Waiting short",
    }
    assert quiet.name in names


# -- messages logged on an application also appear on the person -----------------------------------


def test_recording_a_followup_to_a_contact_logs_it_on_both_sides(contacts, tracker, session):
    application = tracker.create_application("Acme", status=Status.APPLIED, at=day(-20))
    sam = contacts.create("Acme", "Sam")
    followup = tracker.record_followup(
        application,
        FollowupKind.FOLLOW_UP,
        text="Hello Sam, any news?",
        at=day(-1),
        contact=sam,
        channel="linkedin",
    )
    (logged,) = sam.interactions
    assert (logged.direction, logged.channel, logged.application_id) == (
        "outbound",
        "linkedin",
        application.id,
    )
    assert logged.followup_id == followup.id and logged.summary == "Hello Sam, any news?"
    assert contacts.state(sam, now=NOW).awaiting


def test_a_contact_from_another_company_cannot_receive_the_followup(contacts, tracker):
    application = tracker.create_application("Acme", status=Status.APPLIED, at=day(-20))
    stranger = contacts.create("Globex", "Alex")
    with pytest.raises(ValueError, match="different company"):
        tracker.record_followup(application, at=day(-1), contact=stranger)
    assert application.followups == [] and application.status is Status.APPLIED


def test_a_bad_channel_is_refused_before_anything_changes(contacts, tracker):
    application = tracker.create_application("Acme", status=Status.APPLIED, at=day(-20))
    sam = contacts.create("Acme", "Sam")
    with pytest.raises(ValueError):
        tracker.record_followup(application, at=day(-1), contact=sam, channel="telegram")
    assert application.status is Status.APPLIED and application.followups == []


def test_a_followup_without_a_contact_creates_no_interaction(tracker, session):
    application = tracker.create_application("Acme", status=Status.APPLIED, at=day(-20))
    tracker.record_followup(application, at=day(-1))
    assert session.scalars(select(Interaction)).all() == []
    assert len(session.scalars(select(Followup)).all()) == 1


def test_deleting_an_application_keeps_the_people_and_what_was_said(contacts, tracker, session):
    application = tracker.create_application("Acme", status=Status.APPLIED, at=day(-20))
    sam = contacts.create("Acme", "Sam")
    tracker.record_followup(application, at=day(-1), contact=sam)
    reminders = ReminderService(session)
    reminders.add(due_on=date(2026, 7, 5), application=application)  # only about the application
    reminders.add(due_on=date(2026, 7, 6), application=application, contact=sam)
    tracker.delete_application(application)
    session.commit()

    assert [c.name for c in session.scalars(select(Contact))] == ["Sam"]
    (kept,) = session.scalars(select(Interaction)).all()
    assert kept.application_id is None and kept.followup_id is not None
    (remaining,) = session.scalars(select(Reminder)).all()
    assert remaining.contact_id == sam.id and remaining.application_id is None


# -- reminders ---------------------------------------------------------------------------------


@pytest.fixture
def reminders(session):
    return ReminderService(session)


def test_a_reminder_needs_something_to_remind_about(reminders, tracker, contacts):
    with pytest.raises(ValueError, match="application or a contact"):
        reminders.add(due_on=date(2026, 7, 1))
    stranger = contacts.create("Globex", "Alex")
    application = tracker.create_application("Acme")
    with pytest.raises(ValueError, match="different company"):
        reminders.add(due_on=date(2026, 7, 1), application=application, contact=stranger)


def test_due_and_upcoming_are_split_by_date_and_done_ones_disappear(reminders, tracker):
    application = tracker.create_application("Acme")
    today = date(2026, 7, 1)
    overdue = reminders.add(due_on=date(2026, 6, 28), application=application, note="Overdue")
    reminders.add(due_on=today, application=application, note="Today")
    reminders.add(due_on=date(2026, 7, 4), application=application, note="Soon")
    reminders.add(due_on=date(2026, 7, 20), application=application, note="Far")

    assert [r.note for r in reminders.due(today)] == ["Overdue", "Today"]
    assert [r.note for r in reminders.upcoming(today, days=7)] == ["Soon"]
    reminders.complete(overdue)
    assert [r.note for r in reminders.due(today)] == ["Today"]


def test_snooze_moves_the_date_from_today_and_reopens(reminders, tracker):
    application = tracker.create_application("Acme")
    reminder = reminders.add(due_on=date(2026, 6, 20), application=application)
    reminders.complete(reminder)
    reminders.snooze(reminder, 3, today=date(2026, 7, 1))
    assert reminder.due_on == date(2026, 7, 4) and reminder.done_at is None
    with pytest.raises(ValueError):
        reminders.snooze(reminder, 0)


def test_add_in_days(reminders, tracker):
    application = tracker.create_application("Acme")
    reminder = reminders.add_in_days(7, today=date(2026, 7, 1), application=application)
    assert reminder.due_on == date(2026, 7, 8)
    with pytest.raises(ValueError, match="between"):
        reminders.add_in_days(-1, application=application)


def test_titles_and_links_describe_the_target(reminders, tracker, contacts):
    application = tracker.create_application("Acme")
    sam = contacts.create("Acme", "Sam")
    for_application = reminders.add(due_on=date(2026, 7, 1), application=application)
    for_contact = reminders.add(due_on=date(2026, 7, 1), contact=sam)
    custom = reminders.add(due_on=date(2026, 7, 1), contact=sam, note="Ask about the team")
    assert reminder_title(for_application) == "Follow up on Acme"
    assert reminder_title(for_contact) == "Follow up with Sam (Acme)"
    assert reminder_title(custom) == "Ask about the team"
    assert reminder_url(for_application) == f"/applications/{application.id}"
    assert reminder_url(for_contact) == f"/contacts/{sam.id}"


# -- finding people without scraping -----------------------------------------------------------


def test_search_links_are_plain_urls_with_the_company_encoded():
    links = search_links("Johnson & Johnson", role="Data Scientist")
    assert len(links) >= 5
    for link in links:
        parts = urlparse(link.url)
        assert parts.scheme == "https" and parts.netloc in ("www.linkedin.com", "www.google.com")
        assert len(parse_qs(parts.query)) == 1  # exactly one parameter: nothing leaks out of it
        assert " " not in link.url
    first = parse_qs(urlparse(links[0].url).query)["keywords"][0]
    assert first.startswith(
        "Johnson & Johnson "
    )  # the ampersand survived as text, not as a separator
    hiring = [
        parse_qs(urlparse(link.url).query)["keywords"][0]
        for link in links
        if "hiring manager" in link.label
    ]
    assert hiring == ["Johnson & Johnson Data Scientist hiring manager"]


def test_search_links_cannot_be_broken_out_of_by_odd_company_names():
    links = search_links('Evil" OR site:example.com\nCorp')
    assert links
    for link in links:
        assert "\n" not in link.url
        query = parse_qs(urlparse(link.url).query)
        assert list(query) in (["keywords"], ["q"])
    google = parse_qs(
        urlparse(
            next(
                link.url
                for link in links
                if link.url.startswith("https://www.google.com/search?q=site")
            )
        ).query
    )["q"][0]
    # The company is still one quoted phrase; the quote inside the name was removed, so the
    # "OR site:" in it is text inside quotes, not a search operator.
    assert '"Evil OR site:example.com Corp"' in google


def test_search_links_for_an_empty_company_are_empty():
    assert search_links("   ") == []


def test_mentions_are_read_straight_from_the_text():
    text = (
        "Write to Talent@Northwind.example or hr@northwind.example.\n"
        "Our recruiter: https://es.linkedin.com/in/sam-rivera/ and https://www.linkedin.com/in/alex-q.\n"
        "Not a profile: https://www.linkedin.com/company/northwind. Again talent@northwind.example"
    )
    found = find_mentions(text)
    assert found.emails == ["talent@northwind.example", "hr@northwind.example"]
    assert found.profile_urls == [
        "https://es.linkedin.com/in/sam-rivera",
        "https://www.linkedin.com/in/alex-q",
    ]
    assert find_mentions("nothing here").emails == []


# -- reply statistics --------------------------------------------------------------------------


def test_outreach_stats_count_people_not_messages(contacts, session):
    answered = contacts.create("Acme", "A", source="search")
    ignored = contacts.create("Acme", "B", source="search")
    referral = contacts.create("Acme", "C", source="referral")
    never = contacts.create("Acme", "D")
    contacts.add_interaction(
        answered, "message", direction="outbound", channel="email", at=day(-10)
    )
    contacts.add_interaction(
        answered, "message", direction="outbound", channel="email", at=day(-8)
    )  # nudge
    contacts.add_interaction(answered, "message", direction="inbound", channel="email", at=day(-6))
    contacts.add_interaction(
        ignored, "message", direction="outbound", channel="linkedin", at=day(-9)
    )
    contacts.add_interaction(
        referral, "message", direction="outbound", channel="linkedin", at=day(-5)
    )
    contacts.add_interaction(
        referral, "message", direction="inbound", channel="linkedin", at=day(-4)
    )
    contacts.add_interaction(never, "note", at=day(-1))

    stats = OutreachStatsService(session, min_sample=3).report(now=NOW)
    assert (stats.people, stats.contacted, stats.responded, stats.awaiting) == (4, 3, 2, 1)
    assert stats.rate.value == pytest.approx(2 / 3)
    assert stats.median_response_days == pytest.approx(2.5)  # 4 days and 1 day
    assert stats.conclusive

    channels = {r.label: (r.contacted, r.responded) for r in stats.by_channel}
    assert channels == {"email": (1, 1), "linkedin": (2, 1)}
    sources = {r.label: (r.contacted, r.responded) for r in stats.by_source}
    assert sources == {"search": (2, 1), "referral": (1, 1)}


def test_outreach_stats_flag_small_samples_and_cope_with_nothing(session, contacts):
    empty = OutreachStatsService(session).report(now=NOW)
    assert (empty.contacted, empty.rate.value, empty.median_response_days) == (0, None, None)
    assert not empty.conclusive

    sam = contacts.create("Acme", "Sam")
    contacts.add_interaction(sam, "message", direction="outbound", at=day(-3))
    stats = OutreachStatsService(session, min_sample=20).report(now=NOW)
    assert stats.contacted == 1 and not stats.conclusive
    assert all(not row.conclusive for row in stats.by_channel)


# -- names in a posting ------------------------------------------------------------------------


POSTING_WITH_PEOPLE = (
    POSTING
    + "\nQuestions? Contact Sam Rivera, Talent Acquisition Partner, or Alex Quinn (Head of Data).\n"
)


def people_json(*people):
    import json

    return json.dumps({"people": [{"name": n, "title": t, "quote": q} for n, t, q in people]})


def test_names_the_posting_states_are_found_with_their_quotes():
    answer = people_json(
        (
            "Sam Rivera",
            "Talent Acquisition Partner",
            "Contact Sam Rivera, Talent Acquisition Partner",
        ),
        ("Alex Quinn", "Head of Data", "Alex Quinn (Head of Data)"),
    )
    found = ContactExtractor(FakeLLM(answer)).find_people(POSTING_WITH_PEOPLE)
    assert [(p.name, p.title) for p in found] == [
        ("Sam Rivera", "Talent Acquisition Partner"),
        ("Alex Quinn", "Head of Data"),
    ]


def test_a_name_the_posting_does_not_contain_is_never_suggested():
    invented = people_json(("Jordan Lee", "CEO", "Contact Jordan Lee, CEO"))
    llm = FakeLLM(invented, invented, invented)
    assert ContactExtractor(llm).find_people(POSTING_WITH_PEOPLE) == []
    assert "not in the posting" in llm.prompts[1]  # the model was told why it was refused


def test_a_real_quote_with_a_different_name_is_refused():
    swapped = people_json(("Jordan Lee", None, "Alex Quinn (Head of Data)"))
    found = ContactExtractor(FakeLLM(swapped), max_retries=0).find_people(POSTING_WITH_PEOPLE)
    assert found == []  # the quote exists, but it does not contain that name


def test_a_posting_that_names_nobody_gives_an_empty_list_without_retrying():
    llm = FakeLLM(people_json())
    assert ContactExtractor(llm).find_people(POSTING) == []
    assert len(llm.prompts) == 1


def test_good_names_survive_when_another_is_invented():
    answer = people_json(
        ("Sam Rivera", None, "Contact Sam Rivera, Talent Acquisition Partner"),
        ("Jordan Lee", None, "Contact Jordan Lee"),
    )
    found = ContactExtractor(FakeLLM(answer), max_retries=0).find_people(POSTING_WITH_PEOPLE)
    assert [p.name for p in found] == ["Sam Rivera"]


def test_the_extractor_gives_up_on_unusable_output():
    with pytest.raises(LLMError):
        ContactExtractor(FakeLLM("nope", "nope", "nope")).find_people(POSTING_WITH_PEOPLE)
    with pytest.raises(ValueError, match="empty"):
        ContactExtractor(FakeLLM()).find_people("  ")
