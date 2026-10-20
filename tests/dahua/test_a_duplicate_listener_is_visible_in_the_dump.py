"""`events.listener_keys` has to show a code subscribed twice, not just subscribed.

#944 is a doorbell event entity firing twice for one press. The reporter answered the
question that narrows it: clean after a Home Assistant restart, duplicating later. That
rules out a leftover registry entity and the device sending two presses, because both
would be there from the first press, and leaves something accumulating while HA runs.
A second listener on one event key is the leading candidate, and this codebase has had
exactly that bug before on the plate listener.

So the reporter was sent to `events.listener_keys` in the diagnostics dump, on the
grounds that "two subscriptions for one code show up there directly". They did not. The
field was `sorted(self._dahua_event_listeners)`, which is the key *names*, and a key
holding two listeners printed identically to one holding a single listener. The only
thing it could confirm was that the doorbell was subscribed at all, which nobody
doubted.

The field now maps each key to how many listeners it holds, so the question it is asked
for is one it can answer. The key names stay visible as the mapping's keys, because the
field's other use is the channel-offset read: listeners on `...-3` while events arrive
with index 0.

One count is never a verdict by itself, and the tests below use a neutral key rather
than the doorbell's so as not to imply otherwise. Two entities legitimately subscribe to
`DoorbellPressed` on a VTO, the "Button Pressed" binary sensor and the "Doorbell" event
entity, so a healthy doorbell reports 2 for it. What the field makes visible is the key
*changing* between a dump taken after a restart and one taken once the duplicates have
started, which is the thing the names could not show at all.
"""

from types import SimpleNamespace

import pytest

import custom_components.dahua as dahua
from custom_components.dahua.diagnostics import _events_block

ADDRESS = "10.0.0.231"


def _coordinator(listeners, **kwargs):
    defaults = {
        "_address": ADDRESS,
        "_event_task": None,
        "_vto_task": None,
        "_vto_client": None,
        "_dahua_event_listeners": listeners,
        "_dahua_event_timestamp": {},
        "_recent_events": [],
        "get_event_list": lambda: ["VideoMotion"],
        "get_channel": lambda: 0,
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


@pytest.fixture(autouse=True)
def _clean_streams():
    """The registry is a module global, so a leaked entry would reach other tests."""
    dahua._HOST_STREAMS.clear()
    yield
    dahua._HOST_STREAMS.clear()


def test_two_listeners_on_one_key_are_counted():
    """The regression test, and the measurement #944 needs.

    Reverting the field to `sorted(keys)` answers `["VideoMotion-0"]` here, which is
    the same answer it gives for one listener.
    """
    coordinator = _coordinator({"VideoMotion-0": [lambda e: None, lambda e: None]})

    assert _events_block(coordinator)["listener_keys"] == {"VideoMotion-0": 2}


def test_one_listener_reads_one():
    """The negative control. Without it, a field hard-coded to report trouble passes
    the test above."""
    coordinator = _coordinator({"VideoMotion-0": [lambda e: None]})

    assert _events_block(coordinator)["listener_keys"] == {"VideoMotion-0": 1}


def test_a_healthy_doorbell_reports_two_and_that_is_not_a_fault():
    """The count is read as a change, not as an absolute, and this is why.

    `binary_sensor.py` creates a "Button Pressed" sensor on `DoorbellPressed` and
    `event.py` registers the "Doorbell" event entity on the same name, so two is what
    a working VTO has. Written down as a test because the field exists to diagnose
    #944, and a reporter who reads 2 and concludes "accumulating" has been sent down
    the same wrong path the old field sent them down, in the other direction."""
    coordinator = _coordinator({"DoorbellPressed-0": [lambda e: None, lambda e: None]})

    assert _events_block(coordinator)["listener_keys"] == {"DoorbellPressed-0": 2}


def test_the_key_names_survive():
    """The field's other job. An NVR subscribing on channel 3 while its events arrive
    with index 0 is read off these names, so counting must not replace them."""
    coordinator = _coordinator(
        {
            "VideoMotion-3": [lambda e: None],
            "CrossLineDetection-3": [lambda e: None],
        }
    )

    assert set(_events_block(coordinator)["listener_keys"]) == {
        "VideoMotion-3",
        "CrossLineDetection-3",
    }


def test_the_keys_are_ordered():
    """A dump is read by eye and compared against another dump. The old field sorted,
    and losing that would make two dumps diff against each other for no reason."""
    coordinator = _coordinator(
        {
            "VideoMotion-0": [lambda e: None],
            "AlarmLocal-0": [lambda e: None],
            "DoorbellPressed-0": [lambda e: None],
        }
    )

    assert list(_events_block(coordinator)["listener_keys"]) == [
        "AlarmLocal-0",
        "DoorbellPressed-0",
        "VideoMotion-0",
    ]


def test_nothing_subscribed_is_empty_rather_than_absent():
    coordinator = _coordinator({})

    assert _events_block(coordinator)["listener_keys"] == {}


def test_a_coordinator_without_the_attribute_does_not_break_the_dump():
    """Diagnostics runs against whatever state an entry is in, including one that
    failed early, and the dump is the thing a reporter is asked for when it did."""
    coordinator = _coordinator(None)
    del coordinator._dahua_event_listeners

    assert _events_block(coordinator)["listener_keys"] == {}


def test_a_value_that_cannot_be_counted_is_reported_as_unknown():
    """Reading a value the field never read before is a new demand on every double
    that holds the key and not the list, and this one took 45 tests in
    test_diagnostics.py down with a TypeError when it went in. Those doubles were
    fixed to hold the real shape, but the dump is what somebody is asked for once
    everything else has already failed, so it reports what it cannot measure rather
    than raising, and rather than inventing a count."""
    coordinator = _coordinator({"VideoMotion-0": object()})

    assert _events_block(coordinator)["listener_keys"] == {"VideoMotion-0": None}


def test_a_none_attribute_does_not_break_the_dump():
    """`getattr(..., {})` returns the None that is really there, and `.items()` on it
    raises. The old `sorted(None)` raised too, so this is not a new hazard, but the
    dump is exactly the wrong place to learn that."""
    coordinator = _coordinator(None)

    assert _events_block(coordinator)["listener_keys"] == {}
