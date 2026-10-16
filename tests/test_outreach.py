import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from helpers import CV, POSTING, REQUIREMENTS, parsed_posting, posting_json
from jobtrail.database.models import Followup
from jobtrail.domain.followup import FollowupKind
from jobtrail.domain.status import Status
from jobtrail.domain.text_match import unsupported_numbers
from jobtrail.llm.base import LLMError
from jobtrail.llm.fake import FakeLLM
from jobtrail.schemas.matching import ParsedPosting
from jobtrail.services.analytics import AnalyticsService
from jobtrail.services.cv_advisor import CVAdvisor
from jobtrail.services.matching import CVService, MatchingService
from jobtrail.services.message_drafter import MessageDrafter

NOW = datetime(2026, 7, 1, 10, 0, tzinfo=UTC)
APPLIED_AT = NOW - timedelta(days=20)


def day(offset: int) -> datetime:
    return NOW + timedelta(days=offset)


@pytest.fixture
def applied(tracker):
    return tracker.create_application(
        "Northwind Labs",
        "Data Scientist",
        status=Status.APPLIED,
        applied_at=APPLIED_AT,
        at=APPLIED_AT,
    )


# -- recording follow-ups ---------------------------------------------------------------------


def test_first_follow_up_also_moves_the_status(tracker, applied):
    followup = tracker.record_followup(applied, text="Hello, any news?", at=day(-1))
    assert applied.status is Status.FOLLOW_UP_SENT
    assert [e.to_status for e in applied.events] == [Status.APPLIED, Status.FOLLOW_UP_SENT]
    assert applied.followups == [followup] and followup.text == "Hello, any news?"


def test_further_follow_ups_extend_the_log_without_a_status_change(tracker, applied):
    tracker.record_followup(applied, at=day(-10))
    tracker.record_followup(applied, at=day(-3))
    assert applied.status is Status.FOLLOW_UP_SENT
    assert len(applied.events) == 2  # no repeated status event
    assert len(applied.followups) == 2


def test_a_follow_up_does_not_resurrect_a_no_response_application(tracker, applied):
    tracker.change_status(applied, Status.NO_RESPONSE, at=day(-5))
    tracker.record_followup(applied, at=day(-1))
    assert applied.status is Status.NO_RESPONSE and len(applied.followups) == 1


@pytest.mark.parametrize(
    ("status", "kind", "ok"),
    [
        (Status.WISHLIST, FollowupKind.FOLLOW_UP, False),
        (Status.APPLIED, FollowupKind.THANK_YOU, False),
        (Status.INTERVIEWING, FollowupKind.THANK_YOU, True),
        (Status.INTERVIEWING, FollowupKind.FOLLOW_UP, False),
        (Status.WISHLIST, FollowupKind.RECRUITER_INTRO, True),
        (Status.APPLIED, FollowupKind.RECRUITER_INTRO, True),
        (Status.REJECTED, FollowupKind.RECRUITER_INTRO, False),
        (Status.WITHDRAWN, FollowupKind.FOLLOW_UP, False),
    ],
)
def test_message_kinds_fit_only_some_statuses(tracker, status, kind, ok):
    app = tracker.create_application("Acme", status=status, at=day(-30))
    if ok:
        tracker.record_followup(app, kind, at=day(0))
        assert len(app.followups) == 1
    else:
        with pytest.raises(ValueError, match="does not fit"):
            tracker.record_followup(app, kind, at=day(0))
        assert app.followups == []


def test_a_follow_up_dated_before_the_application_changes_nothing(tracker, applied):
    with pytest.raises(ValueError, match="before the previous event"):
        tracker.record_followup(applied, at=APPLIED_AT - timedelta(days=3))
    assert applied.status is Status.APPLIED and applied.followups == []


def test_followup_times_must_be_timezone_aware(tracker, applied):
    with pytest.raises(ValueError, match="timezone-aware"):
        tracker.record_followup(applied, at=datetime(2026, 6, 30))


def test_deleting_an_application_deletes_its_follow_ups(tracker, applied, session):
    tracker.record_followup(applied, at=day(-2))
    tracker.delete_application(applied)
    session.commit()
    assert session.scalars(select(Followup)).all() == []


# -- the waiting clock ------------------------------------------------------------------------


def test_a_recent_follow_up_means_the_application_is_not_stale(tracker, applied, session):
    analytics = AnalyticsService(session, stale_after_days=21)
    assert analytics.stale_applications(now=NOW) == []  # 20 days: not yet
    assert [s.days_waiting for s in analytics.stale_applications(now=day(5))] == [25]

    tracker.record_followup(applied, at=day(3))
    assert analytics.stale_applications(now=day(5)) == []  # nudged 2 days ago
    waiting = analytics.stale_applications(now=day(26))
    assert [s.days_waiting for s in waiting] == [23]  # the clock runs from the follow-up


def test_a_second_follow_up_restarts_the_clock_though_the_status_does_not_change(
    tracker, applied, session
):
    analytics = AnalyticsService(session, stale_after_days=21)
    tracker.record_followup(applied, at=day(-10))  # moves the status
    tracker.record_followup(applied, at=day(3))  # only the log changes
    assert len(applied.events) == 2
    assert analytics.stale_applications(now=day(15)) == []  # 12 days since the second nudge
    assert [s.days_waiting for s in analytics.stale_applications(now=day(26))] == [23]


# -- numbers ---------------------------------------------------------------------------------


def test_unsupported_numbers():
    allowed = "Applied 20 days ago. Salary 45.000 EUR. Built dashboards used by 40 users."
    assert unsupported_numbers("I applied 20 days ago", allowed) == []
    assert unsupported_numbers("about 45,000 EUR", allowed) == []  # separators do not matter
    assert unsupported_numbers("I cut costs by 70% and 12 hours", allowed) == ["70", "12"]
    assert unsupported_numbers("no digits here", allowed) == []
    assert unsupported_numbers("70 and 70 again", allowed) == ["70"]


# -- message drafter --------------------------------------------------------------------------


def answer(body, subject="Following up", quotes=()):
    return json.dumps({"subject": subject, "body": body, "cv_quotes": list(quotes)})


GOOD = answer(
    "Hello Sam, I applied 20 days ago and wanted to check in. I built dashboards in Power BI "
    "used by 40 finance users and would love to bring that to your team. Best regards,",
    quotes=["Built dashboards in Power BI used by 40 finance users"],
)


def test_a_grounded_draft_passes_without_warnings(applied):
    llm = FakeLLM(GOOD)
    draft = MessageDrafter(llm).draft(
        applied, FollowupKind.FOLLOW_UP, recipient="Sam", cv_text=CV, now=NOW
    )
    assert draft.warnings == [] and draft.language == "en"
    assert draft.cv_quotes == ["Built dashboards in Power BI used by 40 finance users"]
    prompt = llm.prompts[0]
    assert "Company: Northwind Labs" in prompt and "20 days ago" in prompt
    assert "Recipient: Sam" in prompt and "Power BI" in prompt  # facts and CV reached the model
    assert llm.schemas[0] is not None


def test_numbers_that_are_in_the_facts_or_the_cv_are_allowed(applied):
    draft = MessageDrafter(FakeLLM(GOOD)).draft(
        applied, FollowupKind.FOLLOW_UP, recipient="Sam", cv_text=CV, now=NOW
    )
    assert draft.warnings == []  # 20 (days) and 40 (CV) are both supported


def test_an_invented_number_is_sent_back_then_fixed(applied):
    invented = answer("Hello, I applied 20 days ago. I cut reporting costs by 70% last year.")
    clean = answer("Hello, I applied 20 days ago and would appreciate an update. Best regards,")
    llm = FakeLLM(invented, clean)
    draft = MessageDrafter(llm).draft(applied, FollowupKind.FOLLOW_UP, now=NOW)
    assert "70" in llm.prompts[1] and "REJECTED" in llm.prompts[1]
    assert draft.warnings == [] and "70" not in draft.body


def test_on_the_last_attempt_unsupported_content_is_kept_but_flagged(applied):
    bad = answer("Hello, I increased revenue by 300%.", quotes=["Led the revenue team"])
    draft = MessageDrafter(FakeLLM(bad, bad), max_retries=1).draft(
        applied, FollowupKind.FOLLOW_UP, cv_text=CV, now=NOW
    )
    assert len(draft.warnings) == 2
    assert any("300" in w for w in draft.warnings)
    assert any("Check every statement" in w for w in draft.warnings)
    assert draft.cv_quotes == []  # the unverified quote is not presented as evidence


def test_without_a_cv_no_claims_about_experience_are_accepted(applied):
    llm = FakeLLM(
        answer("Hello, I have strong Python skills.", quotes=["strong Python skills"]),
        answer("Hello, I applied 20 days ago and would like an update."),
    )
    draft = MessageDrafter(llm).draft(applied, FollowupKind.FOLLOW_UP, now=NOW)
    assert "say nothing about the candidate's experience" in llm.prompts[0]
    assert draft.cv_quotes == [] and draft.warnings == []
    assert len(llm.prompts) == 2


@pytest.mark.parametrize(
    ("body", "message"),
    [("   ", "empty"), ("word " * 200, "shorter")],
)
def test_empty_or_overlong_bodies_are_retried(applied, body, message):
    llm = FakeLLM(answer(body), answer("Hello, I applied 20 days ago. Thanks."))
    MessageDrafter(llm).draft(applied, FollowupKind.FOLLOW_UP, now=NOW)
    assert message in llm.prompts[1]


def test_draft_language_and_kind_reach_the_prompt(applied):
    llm = FakeLLM(answer("Hola, solicité hace 20 días."))
    MessageDrafter(llm).draft(applied, FollowupKind.FOLLOW_UP, language="es", now=NOW)
    assert "Write in Spanish" in llm.prompts[0]
    with pytest.raises(ValueError, match="Language"):
        MessageDrafter(FakeLLM()).draft(applied, FollowupKind.FOLLOW_UP, language="fr")


def test_each_kind_has_its_own_instructions(tracker):
    app = tracker.create_application("Acme", status=Status.INTERVIEWING, at=day(-10))
    llm = FakeLLM(answer("Thank you for your time."))
    MessageDrafter(llm).draft(app, FollowupKind.THANK_YOU, now=NOW)
    assert "thank-you note" in llm.prompts[0]


def test_facts_mention_earlier_messages(tracker, applied):
    tracker.record_followup(applied, at=day(-8))
    llm = FakeLLM(answer("Hello again. It has been 8 days since my last note."))
    MessageDrafter(llm).draft(applied, FollowupKind.FOLLOW_UP, now=NOW)
    assert "Messages already sent about this application: 1" in llm.prompts[0]
    assert "Days since the last activity: 8" in llm.prompts[0]


def test_drafts_give_up_cleanly_when_the_model_is_unusable(applied):
    with pytest.raises(LLMError):
        MessageDrafter(FakeLLM("nope", "nope", "nope")).draft(
            applied, FollowupKind.FOLLOW_UP, now=NOW
        )


def test_a_tool_the_cv_does_not_have_is_sent_back_then_fixed(applied):
    fabricated = answer(
        "Hello Sam, I applied 20 days ago. I have experience deploying models with Kubernetes."
    )
    clean = answer("Hello Sam, I applied 20 days ago and would like an update.")
    llm = FakeLLM(fabricated, clean)
    draft = MessageDrafter(llm).draft(
        applied, FollowupKind.FOLLOW_UP, recipient="Sam", cv_text=CV, now=NOW
    )
    assert "Kubernetes" in llm.prompts[1] and "not in the facts or the CV" in llm.prompts[1]
    assert draft.warnings == []


def test_a_fabricated_tool_on_the_last_attempt_is_flagged(applied):
    fabricated = answer("Hello Sam, I have deployed models with Kubernetes. 20 days ago I applied.")
    draft = MessageDrafter(FakeLLM(fabricated), max_retries=0).draft(
        applied, FollowupKind.FOLLOW_UP, recipient="Sam", cv_text=CV, now=NOW
    )
    assert any("Kubernetes" in w for w in draft.warnings)


def test_the_posting_requirements_are_not_given_to_the_drafter(session, tracker, applied):
    MatchingService(session, FakeLLM(posting_json())).add_posting(
        POSTING, application_id=applied.id
    )
    session.refresh(applied)
    llm = FakeLLM(answer("Hello, I applied 20 days ago and would like an update."))
    MessageDrafter(llm).draft(applied, FollowupKind.FOLLOW_UP, now=NOW)
    prompt = llm.prompts[0]
    assert "Kubernetes" not in prompt and "Spark" not in prompt  # so it cannot claim them
    assert "Location: Madrid" in prompt  # harmless context is still there


# -- CV advisor -------------------------------------------------------------------------------


def advice_json(highlights=(), keywords=()):
    return json.dumps(
        {
            "highlights": [
                {"id": i, "cv_quote": q, "placement": p, "why": "relevant"}
                for i, q, p in highlights
            ],
            "keywords": list(keywords),
        }
    )


GOOD_ADVICE = advice_json(
    [
        (2, "Automated monthly reporting with Python and SQL", "experience"),
        (5, "Built dashboards in Power BI used by 40 finance users", "summary"),
    ],
    keywords=["Python", "SQL", "Power BI"],
)


def test_advice_points_at_verified_cv_lines_and_reports_the_rest_as_not_evidenced():
    advice = CVAdvisor(FakeLLM(GOOD_ADVICE)).advise(CV, parsed_posting())
    assert [(h.id, h.placement) for h in advice.highlights] == [(2, "experience"), (5, "summary")]
    assert advice.highlights[0].requirement == "Python and SQL"
    assert advice.keywords == ["Python", "SQL", "Power BI"]
    # Computed by code, not by the model: everything without a verified highlight.
    assert [n.text for n in advice.not_evidenced] == [
        "3+ years in data science",
        "Java",
        "Kubernetes",
        "Spark",
    ]
    assert advice.dropped == 0


def test_an_invented_cv_line_is_sent_back_then_fixed():
    bad = advice_json([(3, "Wrote Java microservices for 5 years", "experience")])
    llm = FakeLLM(bad, GOOD_ADVICE)
    advice = CVAdvisor(llm).advise(CV, parsed_posting())
    assert "not in the CV" in llm.prompts[1] and "Wrote Java microservices" in llm.prompts[1]
    assert advice.dropped == 0 and len(advice.highlights) == 2


def test_on_the_last_attempt_invented_lines_are_dropped_and_counted():
    bad = advice_json(
        [
            (3, "Wrote Java microservices for 5 years", "experience"),
            (2, "Automated monthly reporting with Python and SQL", "experience"),
        ]
    )
    advice = CVAdvisor(FakeLLM(bad), max_retries=0).advise(CV, parsed_posting())
    assert advice.dropped == 1
    assert [h.id for h in advice.highlights] == [2]
    assert "Java" in [n.text for n in advice.not_evidenced]  # still flagged as not evidenced


def test_java_cannot_be_evidenced_by_javascript():
    bad = advice_json([(3, "Java", "skills")])
    advice = CVAdvisor(FakeLLM(bad), max_retries=0).advise(CV, parsed_posting())
    assert advice.highlights == [] and advice.dropped == 1


def test_unknown_and_duplicate_requirement_ids_are_ignored():
    answer_ = advice_json(
        [
            (99, "Python, SQL, pandas, scikit-learn", "skills"),
            (2, "Python, SQL, pandas, scikit-learn", "skills"),
            (2, "Automated monthly reporting with Python and SQL", "experience"),
        ]
    )
    advice = CVAdvisor(FakeLLM(answer_)).advise(CV, parsed_posting())
    assert [(h.id, h.cv_quote) for h in advice.highlights] == [
        (2, "Python, SQL, pandas, scikit-learn")
    ]


def test_keywords_must_be_in_both_the_cv_and_the_posting():
    answer_ = advice_json([], keywords=["Python", "Kubernetes", "FastAPI", "python", "Power BI"])
    advice = CVAdvisor(FakeLLM(answer_)).advise(CV, parsed_posting())
    # Kubernetes: only in the posting. FastAPI: only in the CV. "python" repeats "Python".
    assert advice.keywords == ["Python", "Power BI"]


def test_advice_needs_requirements():
    with pytest.raises(ValueError, match="no requirements"):
        CVAdvisor(FakeLLM()).advise(CV, ParsedPosting())


def test_advice_is_stored_with_the_fit_analysis(session):
    from helpers import HONEST, POSTING

    llm = FakeLLM(posting_json(), HONEST, GOOD_ADVICE)
    service = MatchingService(session, llm)
    cv, _ = CVService(session).add_text("v1", CV)
    analysis = service.analyze(service.add_posting(POSTING), cv)
    service.advise(analysis)
    session.commit()
    session.expire_all()
    stored = session.get(type(analysis), analysis.id).detail
    assert stored["score"] == 50 and len(stored["advice"]["highlights"]) == 2  # fit data kept
    assert REQUIREMENTS[1][0] == "Python and SQL"


# -- the advisor follows the fit analysis ------------------------------------------------------


def fit_items(**status_by_id):
    from jobtrail.schemas.matching import FitItem

    items = []
    for n, (text, kind, _quote) in enumerate(REQUIREMENTS, start=1):
        status = status_by_id.get(f"r{n}", "missing")
        items.append(
            FitItem(
                id=n,
                text=text,
                kind=kind,
                status=status,
                claimed_status=status,
                quote=None,
                verified=True,
            )
        )
    return items


def test_advice_is_limited_to_requirements_the_fit_analysis_supported():
    # The model offers a real CV line for Java (a requirement the CV does not meet).
    offered = advice_json(
        [
            (2, "Automated monthly reporting with Python and SQL", "experience"),
            (3, "Local LLM agent platform: multi-agent pipeline on Ollama", "projects"),
        ]
    )
    llm = FakeLLM(offered)
    items = fit_items(r2="met", r5="partial")
    advice = CVAdvisor(llm).advise(CV, parsed_posting(), items)

    assert [h.id for h in advice.highlights] == [2]  # Java (3) is ignored
    assert "Java" in [n.text for n in advice.not_evidenced]  # and it stays flagged
    assert "Kubernetes" in [n.text for n in advice.not_evidenced]
    prompt = llm.prompts[0]
    assert "Python and SQL" in prompt and "Power BI" in prompt
    # Requirements 3 (Java) and 4 (Kubernetes) are never offered to the model. (The CV itself
    # contains the word "JavaScript", so check the requirement lines, not the substring.)
    assert "3. [must]" not in prompt and "4. [must]" not in prompt
    assert "2. [must]" in prompt and "5. [nice]" in prompt


def test_when_nothing_is_supported_the_model_is_not_even_called():
    llm = FakeLLM()  # an unexpected call would raise
    advice = CVAdvisor(llm).advise(CV, parsed_posting(), fit_items())
    assert advice.highlights == [] and len(advice.not_evidenced) == 6
    assert llm.prompts == []
