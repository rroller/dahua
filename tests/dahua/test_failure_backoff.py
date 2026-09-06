"""A device that is not answering must be asked less often, not just as often.

Every request costs a Dahua device a connection and a login it has to refuse.
Polling a device that has run out of connections on the configured cadence is
what keeps it out of connections, so these pin the backoff.
"""

import statistics
from datetime import timedelta
from types import SimpleNamespace

from custom_components.dahua import (
    EVENT_STREAM_RETRY_SECONDS,
    FAILURES_BEFORE_BACKOFF,
    POLL_BACKOFF_CAP,
    DahuaDataUpdateCoordinator,
    event_stream_retry_delay,
    failure_backoff,
)

BASE = timedelta(seconds=30)


def _coordinator(**options):
    """A coordinator with only what the interval helpers touch."""
    c = object.__new__(DahuaDataUpdateCoordinator)
    c.config_entry = SimpleNamespace(data={}, options=dict(options))
    c.update_interval = BASE
    c._address = "192.168.0.99"
    return c


# --- a blip is not an outage --------------------------------------------------

def test_a_single_failure_does_not_change_the_interval():
    """Backing off on one timeout would make everything feel sluggish."""
    assert failure_backoff(BASE, 1) == BASE


def test_failures_up_to_the_threshold_keep_the_configured_interval():
    for n in range(1, FAILURES_BEFORE_BACKOFF + 1):
        assert failure_backoff(BASE, n) == BASE, f"{n} failures already backed off"


# --- past that, ask less often ------------------------------------------------

def test_the_interval_grows_once_the_failures_persist():
    first = failure_backoff(BASE, FAILURES_BEFORE_BACKOFF + 1)
    second = failure_backoff(BASE, FAILURES_BEFORE_BACKOFF + 2)

    assert first > BASE, "a device that keeps failing is still polled as often"
    assert second > first, "the interval is not growing with the failures"


def test_the_interval_is_capped():
    """A device that recovers must not stay missing for an afternoon."""
    assert failure_backoff(BASE, 500) == POLL_BACKOFF_CAP


def test_a_long_configured_interval_is_never_shortened():
    """The cap is a ceiling on backoff, not an override of the user's choice."""
    base = POLL_BACKOFF_CAP * 2
    assert failure_backoff(base, 500) >= base


def test_an_outage_costs_the_device_far_fewer_requests():
    """The point of all this: count the polls a wedged device actually gets."""
    window = timedelta(hours=1).total_seconds()

    def polls_in_window():
        elapsed, count = 0.0, 0
        while elapsed < window:
            count += 1
            elapsed += failure_backoff(BASE, count).total_seconds()
        return count

    without_backoff = window / BASE.total_seconds()  # what it used to do

    assert polls_in_window() * 4 < without_backoff, (
        "backing off is not meaningfully reducing the load on a failing device"
    )


# --- recovery -----------------------------------------------------------------

def test_answering_again_restores_the_configured_interval():
    c = _coordinator()
    c._back_off_poll_interval(FAILURES_BEFORE_BACKOFF + 3)
    assert c.update_interval > BASE

    c._restore_poll_interval()

    assert c.update_interval == BASE


def test_recovery_honours_an_interval_changed_during_the_outage():
    """The user's new value wins over whatever we were doubling from."""
    c = _coordinator()
    c._back_off_poll_interval(FAILURES_BEFORE_BACKOFF + 3)

    c.config_entry.options["scan_interval"] = 300
    c._restore_poll_interval()

    assert c.update_interval == timedelta(seconds=300)


def test_backing_off_doubles_the_users_interval_not_the_default():
    c = _coordinator(scan_interval=120)
    c._back_off_poll_interval(FAILURES_BEFORE_BACKOFF + 1)

    assert c.update_interval > timedelta(seconds=120)


# --- the event stream ---------------------------------------------------------

def _mean_delay(lived, failures, samples=300):
    """The delay is jittered, so a single sample compares two coin flips."""
    return statistics.fmean(
        event_stream_retry_delay(lived, failures) for _ in range(samples)
    )


def test_a_stream_that_dies_on_contact_is_retried_less_often():
    """Attaching costs a connection too, so a refused attach must back off."""
    once = _mean_delay(0.5, 1)
    twice = _mean_delay(0.5, 2)
    later = _mean_delay(0.5, 5)

    # Each step doubles and the jitter is a tenth, so a real doubling clears
    # 1.5x comfortably while an unchanged delay cannot reach it.
    assert twice > once * 1.5, "a stream refused twice is retried just as fast"
    assert later > twice * 1.5


def test_the_first_failure_still_waits_the_old_delay():
    """Behaviour for a one-off failure should be unchanged."""
    spread = EVENT_STREAM_RETRY_SECONDS * 0.2
    assert abs(event_stream_retry_delay(0.5, 1) - EVENT_STREAM_RETRY_SECONDS) <= spread


def test_a_stream_that_lived_is_still_reconnected_at_once():
    """Backoff must not slow down the healthy hourly recycle."""
    assert event_stream_retry_delay(3600, 0) == 0.0
    assert event_stream_retry_delay(3600, 9) == 0.0
