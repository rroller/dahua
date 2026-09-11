"""A subscription that attached and said nothing never lived, however long it sat.

The stream asks the device to heartbeat every EVENT_STREAM_HEARTBEAT_SECONDS and
gives up on the socket after EVENT_STREAM_READ_TIMEOUT_SECONDS of silence. That
read timeout raises aiohttp's ServerTimeoutError, which **is** a TimeoutError --
the very exception the deliberate hourly recycle raises through wait_for. Both
landed in the branch meaning the stream had been working, so a subscription
delivering nothing was recorded as healthy: no warning, counters cleared, and
reconnected at once, indefinitely.

Duration cannot separate them. Whether the device ever spoke can, and
`stream_lifetime` is where that judgement is made, before the retry delay sees it.
"""

from custom_components.dahua import (
    EVENT_STREAM_HEALTHY_SECONDS,
    event_stream_retry_delay,
    stream_lifetime,
)


# --- the judgement itself -----------------------------------------------------

def test_a_stream_that_never_spoke_did_not_live():
    """An hour of silence is the read timeout firing over and over, not uptime."""
    assert stream_lifetime(3600.0, received_data=False) == 0.0


def test_the_read_timeout_is_not_mistaken_for_the_recycle():
    """Sixty seconds is exactly where the two collide."""
    assert stream_lifetime(EVENT_STREAM_HEALTHY_SECONDS, received_data=False) == 0.0


def test_a_stream_that_spoke_keeps_its_lifetime():
    assert stream_lifetime(3600.0, received_data=True) == 3600.0
    assert stream_lifetime(8.0, received_data=True) == 8.0


# --- and what it means once the delay sees it ---------------------------------

def test_a_silent_stream_is_not_reconnected_immediately():
    """The bug: a mute subscription reconnecting every read timeout, forever."""
    lived = stream_lifetime(EVENT_STREAM_HEALTHY_SECONDS, received_data=False)
    assert event_stream_retry_delay(lived, 1, received_data=False) > 0


def test_a_persistently_silent_stream_backs_off():
    """It delivers nothing, so waiting longer costs nothing and saves logins."""
    lived = stream_lifetime(3600.0, received_data=False)
    first = event_stream_retry_delay(lived, 1, received_data=False)
    later = event_stream_retry_delay(lived, 5, received_data=False)
    assert later > first


def test_a_delivering_stream_is_untouched():
    """The healthy paths must not have moved."""
    lived = stream_lifetime(3600.0, received_data=True)
    assert event_stream_retry_delay(lived, 0, received_data=True) == 0.0
