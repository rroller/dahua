"""A doorbell speaking a dialect we do not read should say so.

Only states 1 and 2 count as ringing, and the comment beside that set has
always conceded the values vary by model. Everything else was treated as "not
ringing" **silently**: a doorbell reporting its ring as some other number
produced no button press, no error, and nothing in the log explaining it.

That is the missing piece in a long row of issues, all of the shape "my button
press does not work" with no way to tell whether the device is quiet or is
speaking a dialect we do not read: #175, #250, #329, #358, #417, #556, #564,
#593, #690. Every one needed this number, and the only way to get it was to
turn on debug logging and read raw events.

What must not change: the state is still treated as not-ringing. This adds
visibility, not new behaviour, because inventing a ring from a state nobody has
identified would raise false presses on every device that reports it.
"""
from types import SimpleNamespace

import pytest

from custom_components.dahua import (
    DOORBELL_RINGING_STATES,
    DOORBELL_STATE_EVENTS,
    DahuaDataUpdateCoordinator,
)


def _coordinator():
    c = object.__new__(DahuaDataUpdateCoordinator)
    c._unknown_doorbell_states = set()
    c.get_device_name = lambda: "Front Door"
    return c


def _complaints(caplog):
    return [r for r in caplog.records if "does not recognise" in r.getMessage()]


# --- the gap ----------------------------------------------------------------

def test_an_unknown_state_is_reported(caplog):
    c = _coordinator()

    c._note_unknown_doorbell_state(3, "3")

    said = _complaints(caplog)
    assert len(said) == 1
    assert "3" in said[0].getMessage()
    assert "Front Door" in said[0].getMessage()


def test_the_message_says_where_to_report_it(caplog):
    c = _coordinator()

    c._note_unknown_doorbell_state(12, "12")

    assert "github.com/rroller/dahua/issues" in _complaints(caplog)[0].getMessage()


def test_a_state_that_is_not_a_number_is_still_reported(caplog):
    """Whatever the device sent is worth seeing, even if it is not an integer."""
    c = _coordinator()

    c._note_unknown_doorbell_state(None, "Ringing")

    assert "Ringing" in _complaints(caplog)[0].getMessage()


# --- not once per ring ------------------------------------------------------

def test_the_same_state_is_reported_only_once(caplog):
    """A doorbell reports its state on every call; a warning per ring would be
    worse than the bug."""
    c = _coordinator()

    for _ in range(5):
        c._note_unknown_doorbell_state(3, "3")

    assert len(_complaints(caplog)) == 1


def test_a_different_state_is_reported_separately(caplog):
    c = _coordinator()

    c._note_unknown_doorbell_state(3, "3")
    c._note_unknown_doorbell_state(12, "12")

    assert len(_complaints(caplog)) == 2


# --- what must stay quiet ---------------------------------------------------

def test_idle_is_not_worth_reporting(caplog):
    """0 is how a call normally ends. Complaining about it would fire on
    every doorbell in existence, every time."""
    c = _coordinator()

    c._note_unknown_doorbell_state(0, "0")

    assert _complaints(caplog) == []


@pytest.mark.parametrize("state", sorted(DOORBELL_STATE_EVENTS))
def test_the_unlock_states_are_handled_elsewhere(state, caplog):
    """8 and 9 are the unlock results and are dispatched as their own events,
    so they are neither a ring nor unknown."""
    c = _coordinator()

    c._note_unknown_doorbell_state(state, str(state))

    assert _complaints(caplog) == []


def test_the_state_sets_do_not_overlap():
    """The invariant this rests on.

    A state cannot be both a ring and an unlock result, and 0 cannot be either,
    or the guard above would swallow something real.
    """
    assert not (DOORBELL_RINGING_STATES & set(DOORBELL_STATE_EVENTS))
    assert 0 not in DOORBELL_RINGING_STATES
    assert 0 not in DOORBELL_STATE_EVENTS
