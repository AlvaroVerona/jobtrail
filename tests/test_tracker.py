from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from jobtrail.database.models import Channel, Company, StatusEvent
from jobtrail.domain.status import InvalidTransition, Status
from jobtrail.services.tracker import ApplicationNotFound

T0 = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)


def test_default_channels_are_seeded(session):
    names = set(session.scalars(select(Channel.name)))
    assert {"Easy Apply", "Company website", "Referral"} <= names


def test_create_application_records_initial_event(tracker):
    app = tracker.create_application("Acme", "Data Scientist", at=T0)
    assert app.status is Status.WISHLIST
    events = tracker.history(app)
    assert [(e.from_status, e.to_status, e.at) for e in events] == [(None, Status.WISHLIST, T0)]


def test_creating_mid_funnel_sets_applied_at(tracker):
    app = tracker.create_application("Acme", status=Status.APPLIED, at=T0)
    assert app.applied_at == T0


def test_wishlist_application_has_no_applied_at(tracker):
    assert tracker.create_application("Acme").applied_at is None


def test_company_names_are_deduplicated_ignoring_case(tracker, session):
    first = tracker.create_application("ING")
    second = tracker.create_application("  ing ")
    assert first.company_id == second.company_id
    assert len(list(session.scalars(select(Company)))) == 1


def test_database_enforces_company_uniqueness_ignoring_case(session):
    session.add(Company(name="Acme"))
    session.flush()
    session.add(Company(name="ACME"))
    with pytest.raises(IntegrityError):
        session.flush()


def test_empty_company_name_is_rejected(tracker):
    with pytest.raises(ValueError, match="empty"):
        tracker.create_application("   ")


def test_channel_and_track_are_created_on_first_use_and_reused(tracker):
    a = tracker.create_application("A", channel="Company website", track="Data Science/AI")
    b = tracker.create_application("B", channel="company website", track="data science/ai")
    assert a.channel_id == b.channel_id
    assert a.track_id == b.track_id


def test_full_happy_path_builds_ordered_history(tracker):
    app = tracker.create_application("Acme", at=T0)
    path = [Status.TO_APPLY, Status.APPLIED, Status.SCREENING, Status.INTERVIEWING, Status.OFFER]
    for i, status in enumerate(path, start=1):
        tracker.change_status(app, status, at=T0 + timedelta(days=i))

    assert app.status is Status.OFFER
    assert [e.to_status for e in tracker.history(app)] == [Status.WISHLIST, *path]
    assert app.applied_at == T0 + timedelta(days=2)


def test_change_status_records_from_status_and_note(tracker):
    app = tracker.create_application("Acme", status=Status.APPLIED, at=T0)
    event = tracker.change_status(
        app, Status.REJECTED, at=T0 + timedelta(days=3), note="Automated email"
    )
    assert (event.from_status, event.to_status, event.note) == (
        Status.APPLIED,
        Status.REJECTED,
        "Automated email",
    )


def test_invalid_transition_changes_nothing(tracker):
    app = tracker.create_application("Acme", at=T0)
    with pytest.raises(InvalidTransition):
        tracker.change_status(app, Status.OFFER, at=T0 + timedelta(days=1))
    assert app.status is Status.WISHLIST
    assert len(tracker.history(app)) == 1


def test_status_change_before_previous_event_is_rejected(tracker):
    app = tracker.create_application("Acme", at=T0)
    with pytest.raises(ValueError, match="before the previous event"):
        tracker.change_status(app, Status.TO_APPLY, at=T0 - timedelta(days=1))
    assert app.status is Status.WISHLIST


def test_late_reply_after_no_response_is_recordable(tracker):
    app = tracker.create_application("Acme", status=Status.APPLIED, at=T0)
    tracker.change_status(app, Status.NO_RESPONSE, at=T0 + timedelta(days=30))
    tracker.change_status(app, Status.SCREENING, at=T0 + timedelta(days=40))
    assert app.status is Status.SCREENING


def test_history_survives_a_round_trip_through_the_database(tracker, session):
    app = tracker.create_application("Acme", at=T0)
    tracker.change_status(app, Status.TO_APPLY, at=T0 + timedelta(hours=1))
    session.commit()
    session.expire_all()

    reloaded = tracker.get_application(app.id)
    assert reloaded.status is Status.TO_APPLY
    assert [e.at for e in reloaded.events] == [T0, T0 + timedelta(hours=1)]
    assert all(e.at.tzinfo is not None for e in reloaded.events)


def test_naive_datetimes_are_refused(tracker):
    naive = datetime(2026, 6, 1, 9, 0)
    with pytest.raises(ValueError, match="timezone-aware"):
        tracker.create_application("Acme", at=naive)
    app = tracker.create_application("Acme", at=T0)
    with pytest.raises(ValueError, match="timezone-aware"):
        tracker.change_status(app, Status.TO_APPLY, at=naive)
    with pytest.raises(ValueError, match="timezone-aware"):
        tracker.update_application(app, applied_at=naive)


def test_list_applications_filters(tracker):
    tracker.create_application("A", status=Status.APPLIED, track="Data", channel="Easy Apply")
    tracker.create_application("B", status=Status.APPLIED, track="Finance", channel="Referral")
    tracker.create_application("C", track="Data")

    assert len(tracker.list_applications()) == 3
    assert [a.company.name for a in tracker.list_applications(status=Status.APPLIED)] == ["A", "B"]
    assert [a.company.name for a in tracker.list_applications(track="data")] == ["A", "C"]
    assert [a.company.name for a in tracker.list_applications(channel="referral")] == ["B"]
    assert [a.company.name for a in tracker.list_applications(company="c")] == ["C"]
    assert tracker.list_applications(status=Status.OFFER) == []


def test_update_application_edits_details_but_not_status(tracker):
    app = tracker.create_application("Acme")
    tracker.update_application(app, role="ML Engineer", track="AI", salary_expected=50000)
    assert (app.role, app.track.name, app.salary_expected) == ("ML Engineer", "AI", 50000)

    with pytest.raises(ValueError, match="status"):
        tracker.update_application(app, status=Status.OFFER)
    assert app.status is Status.WISHLIST


def test_delete_application_removes_its_history(tracker, session):
    app = tracker.create_application("Acme")
    tracker.change_status(app, Status.TO_APPLY)
    tracker.delete_application(app)
    assert session.scalars(select(StatusEvent)).all() == []
    with pytest.raises(ApplicationNotFound):
        tracker.get_application(app.id)
