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


def test_eleven_streams_land_in_different_seconds():
    """The case this exists for: one NVR, one entry per channel."""
    seconds = {int(jittered(EVENT_STREAM_MAX_LIFETIME_SECONDS)) for _ in range(11)}

    assert len(seconds) == 11


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
