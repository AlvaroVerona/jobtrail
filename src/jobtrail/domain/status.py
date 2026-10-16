"""Application status lifecycle.

The allowed moves live here, in one table, so the rule is easy to read and to test.
Terminal statuses have no outgoing moves: a rejection or a withdrawal is final.
`NO_RESPONSE` is not terminal, because a late reply is common and should be recordable.
"""

from enum import StrEnum


class Status(StrEnum):
    WISHLIST = "wishlist"
    TO_APPLY = "to_apply"
    APPLIED = "applied"
    FOLLOW_UP_SENT = "follow_up_sent"
    SCREENING = "screening"
    INTERVIEWING = "interviewing"
    OFFER = "offer"
    REJECTED = "rejected"
    NO_RESPONSE = "no_response"
    WITHDRAWN = "withdrawn"


ALLOWED_TRANSITIONS: dict[Status, frozenset[Status]] = {
    Status.WISHLIST: frozenset({Status.TO_APPLY, Status.WITHDRAWN}),
    Status.TO_APPLY: frozenset({Status.WISHLIST, Status.APPLIED, Status.WITHDRAWN}),
    Status.APPLIED: frozenset(
        {
            Status.FOLLOW_UP_SENT,
            Status.SCREENING,
            Status.INTERVIEWING,
            Status.REJECTED,
            Status.NO_RESPONSE,
            Status.WITHDRAWN,
        }
    ),
    Status.FOLLOW_UP_SENT: frozenset(
        {
            Status.SCREENING,
            Status.INTERVIEWING,
            Status.REJECTED,
            Status.NO_RESPONSE,
            Status.WITHDRAWN,
        }
    ),
    Status.SCREENING: frozenset(
        {Status.INTERVIEWING, Status.OFFER, Status.REJECTED, Status.WITHDRAWN}
    ),
    Status.INTERVIEWING: frozenset({Status.OFFER, Status.REJECTED, Status.WITHDRAWN}),
    Status.OFFER: frozenset({Status.REJECTED, Status.WITHDRAWN}),
    Status.NO_RESPONSE: frozenset(
        {Status.SCREENING, Status.INTERVIEWING, Status.REJECTED, Status.WITHDRAWN}
    ),
    Status.REJECTED: frozenset(),
    Status.WITHDRAWN: frozenset(),
}

TERMINAL_STATUSES = frozenset(s for s, targets in ALLOWED_TRANSITIONS.items() if not targets)

# Statuses that mean "the application was actually sent".
SUBMITTED_STATUSES = frozenset(
    {
        Status.APPLIED,
        Status.FOLLOW_UP_SENT,
        Status.SCREENING,
        Status.INTERVIEWING,
        Status.OFFER,
        Status.REJECTED,
        Status.NO_RESPONSE,
    }
)


class InvalidTransition(ValueError):
    def __init__(self, source: Status, target: Status):
        allowed = ", ".join(sorted(s.value for s in ALLOWED_TRANSITIONS[source])) or "none"
        super().__init__(
            f"Cannot move from '{source.value}' to '{target.value}' (allowed: {allowed})"
        )
        self.source = source
        self.target = target


def can_transition(source: Status, target: Status) -> bool:
    return target in ALLOWED_TRANSITIONS[source]


def check_transition(source: Status, target: Status) -> None:
    if not can_transition(source, target):
        raise InvalidTransition(source, target)
