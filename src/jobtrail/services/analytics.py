"""Funnel analytics, computed from the status history. No LLM involved.

Definitions (they matter, so they are spelled out):

* **Applied** - the application was actually sent (it has an applied date). Wishlist and
  to-apply rows are not counted, and a later withdrawal does not remove an application
  from the denominator.
* Stages are cumulative and based on what was *ever* reached, not on the current status:
  - **responded**: ever screening, interviewing, offer or rejected (a rejection is a reply);
  - **advanced**: ever screening, interviewing or offer (a positive reply);
  - **interviewing**: ever interviewing or offer;
  - **offer**: ever offer.
* A group is **conclusive** only with at least `min_sample` applications. Every rate carries
  its sample size and a 95% Wilson interval, because "2 of 5" and "40 of 100" are not the
  same evidence.
"""

import math
import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from jobtrail.database.models import Application, utcnow
from jobtrail.domain.status import Status

RESPONDED = frozenset({Status.SCREENING, Status.INTERVIEWING, Status.OFFER, Status.REJECTED})
ADVANCED = frozenset({Status.SCREENING, Status.INTERVIEWING, Status.OFFER})
INTERVIEWED = frozenset({Status.INTERVIEWING, Status.OFFER})
WAITING = frozenset({Status.APPLIED, Status.FOLLOW_UP_SENT})

NO_GROUP = "(none)"


@dataclass(frozen=True)
class Rate:
    successes: int
    n: int
    value: float | None  # None when n == 0
    low: float | None
    high: float | None


def wilson_rate(successes: int, n: int, z: float = 1.96) -> Rate:
    if n == 0:
        return Rate(successes, n, None, None, None)
    p = successes / n
    denominator = 1 + z**2 / n
    centre = (p + z**2 / (2 * n)) / denominator
    half = z * math.sqrt(p * (1 - p) / n + z**2 / (4 * n**2)) / denominator
    return Rate(successes, n, p, max(0.0, centre - half), min(1.0, centre + half))


@dataclass(frozen=True)
class FunnelRow:
    label: str
    applied: int
    responded: int
    advanced: int
    interviewing: int
    offers: int
    response_rate: Rate
    advance_rate: Rate
    median_days_to_response: float | None
    response_time_n: int
    conclusive: bool


@dataclass(frozen=True)
class StaleItem:
    application: Application
    days_waiting: int


@dataclass(frozen=True)
class AnalyticsReport:
    overall: FunnelRow
    by_channel: list[FunnelRow]
    by_track: list[FunnelRow]
    stale: list[StaleItem]
    weekly_applications: list[tuple[date, int]]


def last_activity(application: Application) -> datetime:
    """When something last happened on the application: a status change or a sent message."""
    moments = [application.events[-1].at, *(f.sent_at for f in application.followups)]
    return max(moments)


def _ever(application: Application, statuses: frozenset[Status]) -> bool:
    return any(event.to_status in statuses for event in application.events)


def _days_to_response(application: Application) -> float | None:
    """Days from applying to the first reply, only when that reply has a real date.

    An application whose first (and only) event is its final status, as an import without
    a response date produces, has a known outcome but an unknown reply date; including it
    would report a fake 0-day response."""
    if application.applied_at is None:
        return None
    for index, event in enumerate(application.events):
        if event.to_status in RESPONDED:
            if index == 0 or event.at < application.applied_at:
                return None
            return (event.at - application.applied_at).total_seconds() / 86400
    return None


class AnalyticsService:
    def __init__(self, session: Session, *, min_sample: int = 20, stale_after_days: int = 21):
        self.session = session
        self.min_sample = min_sample
        self.stale_after_days = stale_after_days

    def _applications(self) -> list[Application]:
        query = select(Application).options(
            selectinload(Application.events),
            selectinload(Application.followups),
            selectinload(Application.channel),
            selectinload(Application.track),
            selectinload(Application.company),
        )
        return list(self.session.scalars(query))

    def _row(self, label: str, applications: list[Application]) -> FunnelRow:
        sent = [a for a in applications if a.applied_at is not None]
        responded = [a for a in sent if _ever(a, RESPONDED)]
        advanced = [a for a in sent if _ever(a, ADVANCED)]
        interviewing = [a for a in sent if _ever(a, INTERVIEWED)]
        offers = [a for a in sent if _ever(a, frozenset({Status.OFFER}))]
        times = [t for a in sent if (t := _days_to_response(a)) is not None]
        return FunnelRow(
            label=label,
            applied=len(sent),
            responded=len(responded),
            advanced=len(advanced),
            interviewing=len(interviewing),
            offers=len(offers),
            response_rate=wilson_rate(len(responded), len(sent)),
            advance_rate=wilson_rate(len(advanced), len(sent)),
            median_days_to_response=statistics.median(times) if times else None,
            response_time_n=len(times),
            conclusive=len(sent) >= self.min_sample,
        )

    def overall(self) -> FunnelRow:
        return self._row("All applications", self._applications())

    def funnel_by(self, dimension: str) -> list[FunnelRow]:
        """Funnel per 'channel' or 'track'. Rows without a value are grouped as '(none)'."""
        if dimension not in ("channel", "track"):
            raise ValueError("dimension must be 'channel' or 'track'")
        groups: dict[str, list[Application]] = defaultdict(list)
        for application in self._applications():
            related = getattr(application, dimension)
            groups[related.name if related else NO_GROUP].append(application)
        rows = [self._row(label, apps) for label, apps in groups.items()]
        return sorted(rows, key=lambda r: (-r.applied, r.label))

    def stale_applications(self, now: datetime | None = None) -> list[StaleItem]:
        """Applications still waiting for a reply for more than `stale_after_days`.
        The clock runs from the latest activity: a status change or a message you sent."""
        now = now or utcnow()
        stale = []
        for application in self._applications():
            if application.status not in WAITING:
                continue
            waiting = (now - last_activity(application)).days
            if waiting > self.stale_after_days:
                stale.append(StaleItem(application, waiting))
        return sorted(stale, key=lambda s: -s.days_waiting)

    def weekly_applications(
        self, now: datetime | None = None, weeks: int = 8
    ) -> list[tuple[date, int]]:
        """Applications sent per week (weeks start on Monday), oldest first, zeros included."""
        now = now or utcnow()
        this_monday = now.astimezone(UTC).date() - timedelta(days=now.weekday())
        starts = [this_monday - timedelta(weeks=i) for i in range(weeks - 1, -1, -1)]
        counts = dict.fromkeys(starts, 0)
        for application in self._applications():
            if application.applied_at is None:
                continue
            applied = application.applied_at.date()
            monday = applied - timedelta(days=applied.weekday())
            if monday in counts:
                counts[monday] += 1
        return list(counts.items())

    def report(self, now: datetime | None = None) -> AnalyticsReport:
        return AnalyticsReport(
            overall=self.overall(),
            by_channel=self.funnel_by("channel"),
            by_track=self.funnel_by("track"),
            stale=self.stale_applications(now),
            weekly_applications=self.weekly_applications(now),
        )
