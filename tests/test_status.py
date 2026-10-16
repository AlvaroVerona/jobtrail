import pytest

from jobtrail.domain.status import (
    ALLOWED_TRANSITIONS,
    TERMINAL_STATUSES,
    InvalidTransition,
    Status,
    can_transition,
    check_transition,
)


def test_every_status_has_a_transition_entry():
    assert set(ALLOWED_TRANSITIONS) == set(Status)


def test_terminal_statuses_are_exactly_rejected_and_withdrawn():
    assert TERMINAL_STATUSES == {Status.REJECTED, Status.WITHDRAWN}


@pytest.mark.parametrize("terminal", sorted(TERMINAL_STATUSES))
def test_terminal_statuses_cannot_move(terminal):
    for target in Status:
        assert not can_transition(terminal, target)


def test_no_status_transitions_to_itself():
    for status in Status:
        assert not can_transition(status, status)


@pytest.mark.parametrize(
    ("source", "target"),
    [
        (Status.WISHLIST, Status.TO_APPLY),
        (Status.TO_APPLY, Status.APPLIED),
        (Status.APPLIED, Status.FOLLOW_UP_SENT),
        (Status.APPLIED, Status.SCREENING),
        (Status.FOLLOW_UP_SENT, Status.NO_RESPONSE),
        (Status.NO_RESPONSE, Status.SCREENING),  # a late reply is recordable
        (Status.SCREENING, Status.INTERVIEWING),
        (Status.INTERVIEWING, Status.OFFER),
        (Status.OFFER, Status.WITHDRAWN),  # declining an offer
    ],
)
def test_expected_moves_are_allowed(source, target):
    assert can_transition(source, target)
    check_transition(source, target)


@pytest.mark.parametrize(
    ("source", "target"),
    [
        (Status.WISHLIST, Status.APPLIED),  # must go through to_apply
        (Status.APPLIED, Status.OFFER),  # cannot skip the interview stage
        (Status.APPLIED, Status.WISHLIST),
        (Status.REJECTED, Status.APPLIED),
        (Status.INTERVIEWING, Status.NO_RESPONSE),
    ],
)
def test_unexpected_moves_are_rejected(source, target):
    assert not can_transition(source, target)
    with pytest.raises(InvalidTransition, match=source.value):
        check_transition(source, target)
