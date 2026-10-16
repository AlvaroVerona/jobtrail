from datetime import UTC, datetime, timedelta

import pytest

from jobtrail.cli import main
from jobtrail.demo import load_demo
from jobtrail.domain.status import Status
from jobtrail.reporting import format_report
from jobtrail.services.analytics import NO_GROUP, AnalyticsService, wilson_rate

D0 = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)


def day(n: int) -> datetime:
    return D0 + timedelta(days=n)


def sent(tracker, company, path, *, channel=None, track=None, applied=0):
    """An application applied on day `applied`, then moved through (status, day) steps."""
    app = tracker.create_application(
        company,
        status=Status.APPLIED,
        applied_at=day(applied),
        at=day(applied),
        channel=channel,
        track=track,
    )
    for status, when in path:
        tracker.change_status(app, status, at=day(when))
    return app


@pytest.fixture
def analytics(session):
    return AnalyticsService(session, min_sample=3, stale_after_days=21)


# -- rates -------------------------------------------------------------------------------------


def test_wilson_rate_with_no_sample_has_no_value():
    rate = wilson_rate(0, 0)
    assert (rate.value, rate.low, rate.high) == (None, None, None)


def test_wilson_interval_reflects_sample_size():
    small = wilson_rate(1, 5)
    large = wilson_rate(20, 100)
    assert small.value == large.value == 0.2
    assert (small.high - small.low) > (large.high - large.low)  # less data, wider interval


@pytest.mark.parametrize(("successes", "n"), [(0, 20), (10, 20), (20, 20)])
def test_wilson_interval_is_valid(successes, n):
    rate = wilson_rate(successes, n)
    assert 0 <= rate.low <= rate.value <= rate.high <= 1


def test_zero_successes_still_leaves_an_upper_bound():
    # "0 of 20" is not "impossible": the true rate could plausibly be up to ~16%.
    assert wilson_rate(0, 20).high == pytest.approx(0.161, abs=0.005)


# -- funnel ------------------------------------------------------------------------------------


def test_funnel_counts_what_was_ever_reached(tracker, analytics):
    sent(tracker, "A", [(Status.SCREENING, 3), (Status.REJECTED, 8)])  # advanced, then rejected
    sent(tracker, "B", [(Status.INTERVIEWING, 4), (Status.OFFER, 12)])
    sent(tracker, "C", [(Status.NO_RESPONSE, 30)])
    sent(tracker, "D", [(Status.REJECTED, 2)])
    sent(tracker, "E", [(Status.WITHDRAWN, 5)])  # sent, then withdrawn: stays in the denominator
    tracker.create_application("F")  # wishlist: never sent
    tracker.create_application("G", status=Status.TO_APPLY)

    row = analytics.overall()
    assert row.applied == 5
    assert row.responded == 3  # A, B, D (a rejection is a reply)
    assert row.advanced == 2  # A, B (D was only rejected)
    assert row.interviewing == 1  # B
    assert row.offers == 1


def test_funnel_stages_are_nested(tracker, analytics):
    sent(tracker, "A", [(Status.SCREENING, 3)])
    sent(tracker, "B", [(Status.INTERVIEWING, 3)])
    sent(tracker, "C", [(Status.REJECTED, 3)])
    row = analytics.overall()
    assert row.applied >= row.responded >= row.advanced >= row.interviewing >= row.offers


def test_rates_use_sent_applications_as_denominator(tracker, analytics):
    sent(tracker, "A", [(Status.SCREENING, 3)])
    sent(tracker, "B", [])
    tracker.create_application("C")  # wishlist must not dilute the rate
    row = analytics.overall()
    assert row.response_rate.n == 2
    assert row.response_rate.value == 0.5


def test_empty_database_gives_zeroes_not_errors(analytics):
    row = analytics.overall()
    assert (row.applied, row.response_rate.value, row.median_days_to_response) == (0, None, None)
    assert not row.conclusive


# -- time to response --------------------------------------------------------------------------


def test_median_time_to_response(tracker, analytics):
    sent(tracker, "A", [(Status.SCREENING, 5)])
    sent(tracker, "B", [(Status.REJECTED, 9)])
    sent(tracker, "C", [(Status.SCREENING, 13)])
    row = analytics.overall()
    assert (row.median_days_to_response, row.response_time_n) == (9, 3)


def test_reply_with_unknown_date_counts_as_response_but_not_in_timing(tracker, analytics):
    sent(tracker, "A", [(Status.SCREENING, 6)])
    # What an import without a response date produces: one event, already at the final status.
    tracker.create_application(
        "B", status=Status.REJECTED, applied_at=day(0), at=day(0), channel="Easy Apply"
    )
    row = analytics.overall()
    assert row.responded == 2
    assert (row.median_days_to_response, row.response_time_n) == (6, 1)  # no fake 0-day reply


# -- grouping and sample size -----------------------------------------------------------------


def test_funnel_by_channel_groups_and_labels_missing_values(tracker, analytics):
    sent(tracker, "A", [], channel="Easy Apply")
    sent(tracker, "B", [], channel="Easy Apply")
    sent(tracker, "C", [(Status.SCREENING, 4)], channel="Referral")
    sent(tracker, "D", [])  # no channel
    rows = analytics.funnel_by("channel")
    assert [(r.label, r.applied) for r in rows] == [
        ("Easy Apply", 2),
        (NO_GROUP, 1),
        ("Referral", 1),
    ]
    referral = rows[2]
    assert referral.response_rate.value == 1.0
    assert not referral.conclusive  # one application proves nothing


def test_funnel_by_track(tracker, analytics):
    sent(tracker, "A", [], track="Data")
    sent(tracker, "B", [], track="Finance")
    assert {r.label for r in analytics.funnel_by("track")} == {"Data", "Finance"}


def test_unknown_dimension_is_rejected(analytics):
    with pytest.raises(ValueError, match="dimension"):
        analytics.funnel_by("company")


def test_group_is_conclusive_only_from_min_sample(tracker, analytics):
    for i in range(2):
        sent(tracker, f"X{i}", [], track="Small")
    for i in range(3):
        sent(tracker, f"Y{i}", [], track="Enough")
    by_label = {r.label: r for r in analytics.funnel_by("track")}
    assert not by_label["Small"].conclusive
    assert by_label["Enough"].conclusive


# -- stale applications ------------------------------------------------------------------------


def test_stale_threshold_is_strictly_more_than_n_days(tracker, analytics):
    sent(tracker, "Exactly21", [])
    sent(tracker, "Day22", [], applied=-1)
    stale = analytics.stale_applications(now=day(21))
    assert [s.application.company.name for s in stale] == ["Day22"]
    assert stale[0].days_waiting == 22


def test_follow_up_restarts_the_stale_clock(tracker, analytics):
    sent(tracker, "Quiet", [])
    sent(tracker, "Chased", [(Status.FOLLOW_UP_SENT, 30)])
    names = [s.application.company.name for s in analytics.stale_applications(now=day(40))]
    assert names == ["Quiet"]


def test_only_waiting_statuses_can_be_stale(tracker, analytics):
    sent(tracker, "Screening", [(Status.SCREENING, 2)])
    sent(tracker, "Rejected", [(Status.REJECTED, 2)])
    sent(tracker, "NoResponse", [(Status.NO_RESPONSE, 2)])
    tracker.create_application("Wishlist", at=day(0))
    assert analytics.stale_applications(now=day(200)) == []


def test_stale_list_is_ordered_longest_wait_first(tracker, analytics):
    sent(tracker, "Newer", [], applied=10)
    sent(tracker, "Older", [], applied=0)
    names = [s.application.company.name for s in analytics.stale_applications(now=day(60))]
    assert names == ["Older", "Newer"]


# -- weekly activity ---------------------------------------------------------------------------


def test_weekly_applications_buckets_by_monday(tracker, analytics):
    now = datetime(2026, 6, 17, 12, 0, tzinfo=UTC)  # a Wednesday; its Monday is 15 June
    for company, applied in [
        ("MonThisWeek", datetime(2026, 6, 15, 8, tzinfo=UTC)),
        ("SunPrevWeek", datetime(2026, 6, 14, 23, tzinfo=UTC)),
        ("MonPrevWeek", datetime(2026, 6, 8, 8, tzinfo=UTC)),
        ("TooOld", datetime(2026, 1, 5, 8, tzinfo=UTC)),
    ]:
        tracker.create_application(company, status=Status.APPLIED, applied_at=applied, at=applied)
    tracker.create_application("Wishlist")

    weeks = analytics.weekly_applications(now=now, weeks=8)
    assert len(weeks) == 8
    assert weeks[-1][0].isoformat() == "2026-06-15"
    assert [w[0] for w in weeks] == sorted(w[0] for w in weeks)
    counts = dict(weeks)
    assert counts[datetime(2026, 6, 15).date()] == 1
    assert counts[datetime(2026, 6, 8).date()] == 2
    assert sum(counts.values()) == 3  # "TooOld" is outside the window, wishlist is not sent


# -- demo data and rendering -------------------------------------------------------------------


def test_demo_data_gives_a_consistent_funnel(session):
    load_demo(session)
    service = AnalyticsService(session)
    for row in [service.overall(), *service.funnel_by("channel"), *service.funnel_by("track")]:
        assert row.applied >= row.responded >= row.advanced >= row.interviewing >= row.offers
    channels = {r.label: r for r in service.funnel_by("channel")}
    assert channels["Referral"].response_rate.value > channels["Easy Apply"].response_rate.value


def test_report_text_warns_about_small_groups(session):
    load_demo(session)
    service = AnalyticsService(session, min_sample=20)
    text = format_report(service.report(), min_sample=20, stale_after_days=21)
    assert "too few to compare" in text
    assert "By channel" in text and "Applications sent per week" in text


def test_report_has_every_section(tracker, session):
    sent(tracker, "A", [])
    report = AnalyticsService(session).report(now=day(60))
    assert report.overall.applied == 1
    assert [s.application.company.name for s in report.stale] == ["A"]
    assert len(report.weekly_applications) == 8


# -- CLI -------------------------------------------------------------------------------------


def test_cli_stats_without_a_database_explains_what_to_do(isolated_data, capsys):
    assert main(["stats"]) == 2
    assert "import-csv" in capsys.readouterr().err
    assert main(["stats", "--demo"]) == 2
    assert "jobtrail demo" in capsys.readouterr().err


def test_cli_stats_demo_prints_the_report(isolated_data, capsys):
    assert main(["demo"]) == 0
    capsys.readouterr()
    assert main(["stats", "--demo"]) == 0
    out = capsys.readouterr().out
    assert "By channel" in out and "Referral" in out
