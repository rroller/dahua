"""A doorbell that is refusing us must be asked less often, like a camera.

The VTO event connection had two fixed delays and no notion of a device that is
refusing: five seconds after a disconnect, thirty after a failure, forever. A
doorbell that had been unplugged was therefore contacted 2,880 times a day,
where the same device as an IP camera would have been backed off to one attempt
every ten minutes.

The trap this has to avoid is the opposite mistake. Most front doors are quiet
for hours, so "no events arrived" cannot mean "this device is refusing" -- which
is why the signal is anything the device says at all, the keepAlive answer
included, and not a doorbell press.
"""

import statistics

from custom_components.dahua import (
    EVENT_STREAM_MAX_RETRY_SECONDS,
    EVENT_STREAM_RETRY_SECONDS,
    EVENT_STREAM_SHORT_RETRY_SECONDS,
    vto_retry_state,
)
from custom_components.dahua.vto import DahuaVTOClient

OLD_FIXED_DELAY = 30


def _mean_delay(lived, failures, received_data, runs=40):
    """The delay averaged over the jitter."""
    return statistics.mean(
        vto_retry_state(lived, failures, received_data)[0] for _ in range(runs))


def test_a_doorbell_that_was_talking_reconnects_at_once():
    """The socket ending is not a refusal when the device was using it."""
    delay, failures = vto_retry_state(3600, 5, True)

    assert delay == 0.0
    assert failures == 0, "a working doorbell kept a failure count"


def test_a_quiet_doorbell_is_not_treated_as_refusing():
    """The whole risk of this change.

    A front door can go hours without an event. What keeps it out of the backoff
    is the keepAlive answer, which reaches `data_received` like anything else.
    """
    delay, failures = vto_retry_state(1800, 3, True)

    assert delay == 0.0
    assert failures == 0


def test_a_doorbell_that_dies_on_contact_backs_off():
    once = _mean_delay(0.2, 0, False)
    twice = _mean_delay(0.2, 1, False)
    later = _mean_delay(0.2, 5, False)

    # A doubling clears 1.5x comfortably; an unchanged delay cannot.
    assert twice > once * 1.5, "a doorbell refusing twice is retried just as fast"
    assert later > twice * 1.5


def test_the_backoff_is_capped():
    assert _mean_delay(0.2, 99, False) <= EVENT_STREAM_MAX_RETRY_SECONDS * 1.2


def test_the_first_refusal_waits_about_a_minute():
    """Not the thirty seconds it used to, and not ten minutes either."""
    assert abs(_mean_delay(0.2, 0, False) - EVENT_STREAM_RETRY_SECONDS) \
        <= EVENT_STREAM_RETRY_SECONDS * 0.2


def test_a_connection_that_talked_then_died_quickly_is_retried_soon():
    """Firmware that hangs up after a few good seconds is working, not refusing."""
    delay, failures = vto_retry_state(5, 4, True)

    assert failures == 0
    assert abs(delay - EVENT_STREAM_SHORT_RETRY_SECONDS) \
        <= EVENT_STREAM_SHORT_RETRY_SECONDS * 0.2


def test_the_failure_count_climbs_while_it_is_refused_and_resets_when_it_answers():
    failures = 0
    for _ in range(4):
        _, failures = vto_retry_state(0.2, failures, False)
    assert failures == 4

    _, failures = vto_retry_state(120, failures, True)
    assert failures == 0


def test_an_unplugged_doorbell_costs_far_fewer_attempts():
    """The number that motivated this: 2,880 attempts a day, forever."""
    day = 24 * 60 * 60
    elapsed, attempts, failures = 0.0, 0, 0
    while elapsed < day:
        delay, failures = vto_retry_state(0.2, failures, False)
        elapsed += delay
        attempts += 1

    assert attempts < day / OLD_FIXED_DELAY / 10, (
        "an unplugged doorbell is still contacted %d times a day" % attempts)


def test_the_protocol_records_that_the_device_spoke():
    """`data_received` is the one entry point for replies, keepalives and events."""
    client = object.__new__(DahuaVTOClient)
    client.host = "10.0.0.1"
    client.buffer = b""
    client.received_data = False

    client.data_received(b"")

    assert client.received_data is True
