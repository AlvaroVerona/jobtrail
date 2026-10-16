"""Outreach that can be logged against an application, and when each kind makes sense."""

from enum import StrEnum

from jobtrail.domain.status import Status


class FollowupKind(StrEnum):
    FOLLOW_UP = "follow_up"  # nudging for an answer after applying
    THANK_YOU = "thank_you"  # after a screening or interview
    RECRUITER_INTRO = "recruiter_intro"  # a first message to a person at the company


ALLOWED_STATUSES: dict[FollowupKind, frozenset[Status]] = {
    FollowupKind.FOLLOW_UP: frozenset({Status.APPLIED, Status.FOLLOW_UP_SENT, Status.NO_RESPONSE}),
    FollowupKind.THANK_YOU: frozenset({Status.SCREENING, Status.INTERVIEWING, Status.OFFER}),
    FollowupKind.RECRUITER_INTRO: frozenset(Status) - {Status.REJECTED, Status.WITHDRAWN},
}

LABELS = {
    FollowupKind.FOLLOW_UP: "Follow-up",
    FollowupKind.THANK_YOU: "Thank-you",
    FollowupKind.RECRUITER_INTRO: "Recruiter intro",
}


def check_kind_for_status(kind: FollowupKind, status: Status) -> None:
    if status not in ALLOWED_STATUSES[kind]:
        allowed = ", ".join(sorted(s.value for s in ALLOWED_STATUSES[kind]))
        raise ValueError(
            f"A {LABELS[kind].lower()} does not fit an application that is '{status.value}' "
            f"(it fits: {allowed})"
        )
