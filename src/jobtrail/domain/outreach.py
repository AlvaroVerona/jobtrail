"""People you contact, and how to tell whether they answered. Pure rules, no I/O."""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol


class Kind(StrEnum):
    MESSAGE = "message"  # an email or a LinkedIn message: it has a direction
    CALL = "call"
    MEETING = "meeting"
    NOTE = "note"  # something worth remembering; not an exchange


class Direction(StrEnum):
    OUTBOUND = "outbound"  # you wrote to them
    INBOUND = "inbound"  # they wrote to you


class Channel(StrEnum):
    EMAIL = "email"
    LINKEDIN = "linkedin"
    PHONE = "phone"
    IN_PERSON = "in_person"
    OTHER = "other"


class Source(StrEnum):
    """How you found the person. It matters later: which sources actually get answers?"""

    POSTING = "posting"
    SEARCH = "search"
    REFERRAL = "referral"
    EVENT = "event"
    OTHER = "other"


LABELS = {
    "email": "Email",
    "linkedin": "LinkedIn",
    "phone": "Phone",
    "in_person": "In person",
    "other": "Other",
    "posting": "The posting",
    "search": "A search",
    "referral": "A referral",
    "event": "An event",
    "message": "Message",
    "call": "Call",
    "meeting": "Meeting",
    "note": "Note",
    "outbound": "Sent",
    "inbound": "Received",
}


def check_interaction(kind: Kind, direction: Direction | None) -> None:
    if kind is Kind.MESSAGE and direction is None:
        raise ValueError("A message needs a direction: sent by you, or received")
    if kind is not Kind.MESSAGE and direction is not None:
        raise ValueError(f"A {kind.value} has no direction")


class Interaction(Protocol):
    kind: str
    direction: str | None
    at: datetime


@dataclass(frozen=True)
class ThreadState:
    contacted: bool  # you have written to them at least once
    responded: bool  # they answered after your first message
    response_days: float | None  # from your first message to their first answer
    awaiting: bool  # the latest exchange is a message from you that nobody answered
    days_waiting: int | None
    last_activity: datetime | None


def _is_exchange(item: Interaction) -> bool:
    return item.kind in (Kind.MESSAGE, Kind.CALL, Kind.MEETING)


def _is_answer(item: Interaction) -> bool:
    """An answer is something they did: a message to you, or a call or meeting that took place."""
    if item.kind == Kind.MESSAGE:
        return item.direction == Direction.INBOUND
    return item.kind in (Kind.CALL, Kind.MEETING)


def thread_state(interactions: Iterable[Interaction], now: datetime) -> ThreadState:
    ordered = sorted(interactions, key=lambda i: i.at)
    if not ordered:
        return ThreadState(False, False, None, False, None, None)

    sent = [i for i in ordered if i.kind == Kind.MESSAGE and i.direction == Direction.OUTBOUND]
    last_activity = ordered[-1].at
    if not sent:
        return ThreadState(False, False, None, False, None, last_activity)

    first = sent[0]
    answers = [i for i in ordered if i.at >= first.at and _is_answer(i)]
    exchanges = [i for i in ordered if _is_exchange(i)]
    latest = exchanges[-1]
    awaiting = latest.kind == Kind.MESSAGE and latest.direction == Direction.OUTBOUND
    return ThreadState(
        contacted=True,
        responded=bool(answers),
        response_days=(answers[0].at - first.at).total_seconds() / 86400 if answers else None,
        awaiting=awaiting,
        days_waiting=(now - latest.at).days if awaiting else None,
        last_activity=last_activity,
    )
