"""Eleven channels of one NVR must not all reconnect in the same second."""

import statistics

from custom_components.dahua import (
    EVENT_STREAM_JITTER,
    EVENT_STREAM_MAX_LIFETIME_SECONDS,
    EVENT_STREAM_RETRY_SECONDS,
    jittered,
)


def test_the_lifetime_is_not_the_same_twice():
    """A fixed lifetime is what synchronises the reconnections."""
    values = {jittered(EVENT_STREAM_MAX_LIFETIME_SECONDS) for _ in range(50)}

    assert len(values) > 40, "the lifetime is barely varying"


def test_the_spread_stays_within_the_configured_fraction():
    spread = EVENT_STREAM_MAX_LIFETIME_SECONDS * EVENT_STREAM_JITTER
    low, high = (
        EVENT_STREAM_MAX_LIFETIME_SECONDS - spread,
        EVENT_STREAM_MAX_LIFETIME_SECONDS + spread,
    )

    for _ in range(200):
        assert low <= jittered(EVENT_STREAM_MAX_LIFETIME_SECONDS) <= high


def test_the_average_is_still_the_interval_asked_for():
    """Jitter should spread the reconnections, not quietly change the period."""
    mean = statistics.fmean(
        jittered(EVENT_STREAM_MAX_LIFETIME_SECONDS) for _ in range(2000)
    )

    assert abs(mean - EVENT_STREAM_MAX_LIFETIME_SECONDS) < (
        EVENT_STREAM_MAX_LIFETIME_SECONDS * 0.02
    )


def test_eleven_streams_do_not_reconnect_together():
    """The case this exists for: one NVR, one entry per channel.

    Not "all eleven differ". Eleven draws over the ~720 seconds this spreads
    them across collide by the birthday problem 7.5% of the time, so asserting
    eleven distinct values failed one CI run in thirteen -- on every pull
    request in the repository, not just ones touching this code.

    What matters is that they are spread at all: without jitter all eleven land
    in the same second, forever. Ten or more distinct is the normal case, and
    over 20000 trials the fewest ever seen was eight.
    """
    seconds = {int(jittered(EVENT_STREAM_MAX_LIFETIME_SECONDS)) for _ in range(11)}

    assert len(seconds) >= 8, "eleven channels are still reconnecting in lockstep"


def test_the_retry_after_a_failure_is_spread_too():
    """A device that rejected every channel would otherwise be retried by
    every channel at the same moment, sixty seconds later."""
    values = {jittered(EVENT_STREAM_RETRY_SECONDS) for _ in range(50)}

    assert len(values) > 40


def test_it_never_returns_something_useless():
    """A tiny or negative delay would turn a retry into a hot loop."""
    for seconds in (1, 5, 60, 3600):
        for _ in range(200):
            assert jittered(seconds) >= 1.0


def test_jitter_can_be_switched_off():
    assert jittered(3600, fraction=0) == 3600


def test_a_zero_interval_is_left_alone():
    assert jittered(0) == 0


# --- backoff after a stream ends --------------------------------------------
#
# The old loop had exactly two branches: under ten seconds wait sixty, otherwise
# reconnect with no delay at all. A device that closes the socket at eleven
# seconds therefore reconnected forever, as fast as it could, in silence.

from custom_components.dahua import (
    EVENT_STREAM_HEALTHY_SECONDS,
    EVENT_STREAM_SHORT_RETRY_SECONDS,
    event_stream_retry_delay,
)


def test_a_stream_that_died_at_once_waits_a_minute():
    assert 50 <= event_stream_retry_delay(0.5) <= 70


def test_a_stream_that_lasted_eleven_seconds_is_not_retried_instantly():
    """This is the case that used to spin with no delay whatsoever."""
    assert event_stream_retry_delay(11) > 0


def test_the_middle_band_waits_about_ten_seconds():
    for lived in (10, 20, 45, 59):
        assert 8 <= event_stream_retry_delay(lived) <= 12


def test_a_healthy_stream_reconnects_immediately():
    """An hour-long stream being recycled must not pause the subscription."""
    assert event_stream_retry_delay(EVENT_STREAM_HEALTHY_SECONDS) == 0.0
    assert event_stream_retry_delay(3600) == 0.0


def test_the_worst_case_reconnect_rate_is_bounded():
    """Cheapest expression of the fix: how often can we possibly re-attach?"""
    worst = min(lived + event_stream_retry_delay(lived) for lived in range(0, 60))

    assert worst >= EVENT_STREAM_SHORT_RETRY_SECONDS, (
        "a device can still be re-attached every %.1fs" % worst
    )
