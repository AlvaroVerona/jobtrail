"""How many of the people you wrote to answered. The unit is the person, not the message.

Same honesty as the application funnel: every rate carries its sample size and a Wilson
interval, and a group with too few people is flagged instead of compared."""

import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from jobtrail.database.models import Contact, utcnow
from jobtrail.domain.outreach import Direction, Kind, thread_state
from jobtrail.services.analytics import Rate, wilson_rate


@dataclass(frozen=True)
class OutreachRow:
    label: str
    contacted: int
    responded: int
    rate: Rate
    conclusive: bool


@dataclass(frozen=True)
class OutreachStats:
    people: int  # everyone you saved
    contacted: int  # people you have written to
    responded: int
    rate: Rate
    median_response_days: float | None
    response_time_n: int
    awaiting: int
    conclusive: bool
    by_channel: list[OutreachRow]
    by_source: list[OutreachRow]


class OutreachStatsService:
    def __init__(self, session: Session, *, min_sample: int = 20):
        self.session = session
        self.min_sample = min_sample

    def _row(self, label: str, contacted: int, responded: int) -> OutreachRow:
        return OutreachRow(
            label,
            contacted,
            responded,
            wilson_rate(responded, contacted),
            contacted >= self.min_sample,
        )

    def report(self, now: datetime | None = None) -> OutreachStats:
        now = now or utcnow()
        contacts = self.session.scalars(
            select(Contact).options(selectinload(Contact.interactions))
        ).all()

        contacted = responded = awaiting = 0
        times: list[float] = []
        channel_groups: dict[str, list[bool]] = defaultdict(list)
        source_groups: dict[str, list[bool]] = defaultdict(list)

        for contact in contacts:
            state = thread_state(contact.interactions, now)
            if state.awaiting:
                awaiting += 1
            if not state.contacted:
                continue
            contacted += 1
            responded += state.responded
            if state.response_days is not None:
                times.append(state.response_days)
            first = next(
                i
                for i in sorted(contact.interactions, key=lambda i: i.at)
                if i.kind == Kind.MESSAGE and i.direction == Direction.OUTBOUND
            )
            channel_groups[first.channel].append(state.responded)
            source_groups[contact.source].append(state.responded)

        def rows(groups: dict[str, list[bool]]) -> list[OutreachRow]:
            built = [self._row(label, len(v), sum(v)) for label, v in groups.items()]
            return sorted(built, key=lambda r: (-r.contacted, r.label))

        return OutreachStats(
            people=len(contacts),
            contacted=contacted,
            responded=responded,
            rate=wilson_rate(responded, contacted),
            median_response_days=statistics.median(times) if times else None,
            response_time_n=len(times),
            awaiting=awaiting,
            conclusive=contacted >= self.min_sample,
            by_channel=rows(channel_groups),
            by_source=rows(source_groups),
        )
