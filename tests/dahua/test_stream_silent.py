"""A subscription that attaches and says nothing is broken, not healthy.

The stream asks the device to heartbeat every EVENT_STREAM_HEARTBEAT_SECONDS and
gives up on the socket after EVENT_STREAM_READ_TIMEOUT_SECONDS of silence. That
read timeout raises aiohttp's ServerTimeoutError, which **is** a TimeoutError --
the same exception the deliberate hourly recycle raises. They landed in the same
branch, so a subscription delivering nothing was recorded as healthy: no warning,
failure counters cleared, and reconnected at once, forever.

Silence is never innocent here. A stream is only started when some event is
wanted, so there is no such thing as a correctly-subscribed device that sends
nothing at all -- a camera with nothing to report still heartbeats.
"""

from custom_components.dahua import (
    EVENT_STREAM_HEALTHY_SECONDS,
    EVENT_STREAM_MAX_RETRY_SECONDS,
    EVENT_STREAM_RETRY_SECONDS,
    event_stream_retry_delay,
)


def test_a_long_silent_stream_is_not_treated_as_healthy():
    """Sixty seconds of silence is the read timeout firing, not a good stream."""
    delay = event_stream_retry_delay(
        EVENT_STREAM_HEALTHY_SECONDS, 1, received_data=False)
    assert delay > 0, "a stream that delivered nothing was reconnected as if healthy"


def test_an_hour_of_silence_is_still_not_healthy():
    """Duration cannot rescue a stream that never said anything."""
    assert event_stream_retry_delay(3600, 1, received_data=False) > 0


def test_a_silent_stream_backs_off_instead_of_retrying_forever():
    """Reconnecting every read timeout costs a login a minute, indefinitely."""
    first = event_stream_retry_delay(60.0, 1, received_data=False)
    later = event_stream_retry_delay(60.0, 5, received_data=False)
    assert later > first, "a permanently silent subscription is not backing off"


def test_the_silent_backoff_is_capped():
    delay = event_stream_retry_delay(60.0, 500, received_data=False)
    assert delay <= EVENT_STREAM_MAX_RETRY_SECONDS * 1.1


def test_silence_and_instant_death_are_treated_the_same():
    """Both mean nothing arrived; the duration between them is not a signal."""
    silent = event_stream_retry_delay(60.0, 1, received_data=False)
    instant = event_stream_retry_delay(0.5, 1, received_data=False)
    assert abs(silent - instant) <= EVENT_STREAM_RETRY_SECONDS * 0.25


def test_a_delivering_stream_is_untouched_by_any_of_this():
    """The healthy paths must not have moved."""
    assert event_stream_retry_delay(3600, 0, received_data=True) == 0.0
    assert 8 <= event_stream_retry_delay(8.0, 3, received_data=True) <= 12
