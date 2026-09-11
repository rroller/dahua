"""A device that talks and then hangs up is not a device refusing to talk.

The retry delay used to decide entirely on how long the stream lasted. That
reads a firmware which closes the socket after eight seconds of good events as
a device dying on contact, and backs it off the same way -- reaching ten minute
retries within about an hour, so a camera that still detects motion reports
almost none of it.

Duration alone cannot tell those apart. Whether the device sent anything can.
"""

from custom_components.dahua import (
    EVENT_STREAM_MAX_RETRY_SECONDS,
    EVENT_STREAM_RETRY_SECONDS,
    EVENT_STREAM_SHORT_RETRY_SECONDS,
    event_stream_retry_delay,
)


# --- the case this exists for -------------------------------------------------

def test_a_short_stream_that_delivered_is_retried_soon():
    """Eight seconds of events then a close: reconnect now, not in a minute."""
    assert 8 <= event_stream_retry_delay(8.0, 1, received_data=True) <= 12


def test_a_talking_device_never_escalates_however_often_it_hangs_up():
    """The backoff must not accumulate against a device that is working."""
    for failures in (1, 5, 20, 500):
        delay = event_stream_retry_delay(8.0, failures, received_data=True)
        assert delay <= 12, f"escalated to {delay:.0f}s after {failures} closes"


def test_the_device_is_not_blinded_for_minutes_at_a_time():
    """The symptom being fixed: long gaps with no stream attached at all."""
    worst = max(event_stream_retry_delay(8.0, n, received_data=True)
                for n in range(1, 50))
    assert worst < 60, f"a working camera goes unwatched for {worst:.0f}s"


# --- and the behaviour that must survive --------------------------------------

def test_a_stream_that_said_nothing_still_backs_off():
    """The hot reconnect loop this backoff was added for is still prevented."""
    first = event_stream_retry_delay(0.5, 1, received_data=False)
    later = event_stream_retry_delay(0.5, 4, received_data=False)

    assert first >= EVENT_STREAM_RETRY_SECONDS * 0.9
    assert later > first, "a device refusing attach is not being backed off"


def test_silence_is_still_the_default():
    """Callers that do not say are treated as before, not as if they delivered.

    Not an equality check against the explicit call: the delay is jittered, so
    two calls with the same arguments differ by design.
    """
    assert event_stream_retry_delay(0.5, 3) > EVENT_STREAM_SHORT_RETRY_SECONDS * 2


def test_the_backoff_is_still_capped():
    delay = event_stream_retry_delay(0.5, 500, received_data=False)
    assert delay <= EVENT_STREAM_MAX_RETRY_SECONDS * 1.1


def test_a_healthy_stream_still_reconnects_at_once():
    """A stream that delivered and lived a healthy while reconnects immediately.

    The `received_data=False` half of this originally asserted 0.0 too, on the
    reasoning that duration alone settles a long-lived stream. It does not: a
    stream that ran an hour and delivered nothing never worked, and treating it
    as healthy is the silent failure test_stream_silent.py covers.
    """
    assert event_stream_retry_delay(3600, 0, received_data=True) == 0.0
