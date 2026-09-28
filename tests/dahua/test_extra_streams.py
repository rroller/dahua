"""How many sub-stream camera entities to create.

`self._max_streams = await self.client.get_max_extra_streams() + 1`, and
camera.py does `for stream_index in range(coordinator.get_max_streams())`. So
this number is a count of camera entities, and guessing it high means entities
that 404 forever:

    Error from stream worker: Error opening stream (HTTP_NOT_FOUND,
    Server returned 404 Not Found)
    rtsp://...:554/cam/realmonitor?channel=1&subtype=2

That is #237, an AD410 owner with a "Sub_2" camera their doorbell does not have.

Measured, so the defaults are not guesses:

    DHI-NVR5464-16P-EI (G61_NVR16PRO16P-I3)   table.MaxExtraStream=2
    VTO2000A doorbell                         table.MaxExtraStream=1

The device that answers 1 is a doorbell, which is exactly #237's shape.
"""

import pytest

from custom_components.dahua.client import DEFAULT_EXTRA_STREAMS, parse_extra_streams


# --- what real devices answer -----------------------------------------------

def test_the_measured_nvr():
    assert parse_extra_streams("2") == 2


def test_the_measured_doorbell_gets_one_sub_stream_not_two():
    """A VTO2000A answers 1. Rounding that up is #237."""
    assert parse_extra_streams("1") == 1


def test_a_device_with_no_sub_streams_at_all():
    assert parse_extra_streams("0") == 0


def test_whitespace_does_not_make_it_unreadable():
    assert parse_extra_streams(" 2 ") == 2


# --- and what an unreadable answer must not do ------------------------------

@pytest.mark.parametrize("value", [None, "", "   ", "abc", "2,3", {}])
def test_an_unreadable_answer_falls_back_rather_than_raising(value):
    """This runs inside the one-time init, whose handler turns any exception
    into UpdateFailed -- so raising here means the entry never initialises and
    retries for as long as the device keeps saying it."""
    assert parse_extra_streams(value) == DEFAULT_EXTRA_STREAMS


def test_a_negative_count_is_nonsense_not_a_smaller_camera():
    assert parse_extra_streams("-1") == DEFAULT_EXTRA_STREAMS


def test_the_fallback_matches_what_the_coordinator_starts_with():
    """self._max_streams = 3 -- one main plus DEFAULT_EXTRA_STREAMS."""
    assert DEFAULT_EXTRA_STREAMS + 1 == 3
