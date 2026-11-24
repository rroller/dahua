"""A Stop that arrives while the event stream is down must not be lost for good.

A latching event's sensor is on for exactly as long as its timestamp is
non-zero, and **only** a Stop zeroes it. So when the stream drops between a
Start and its Stop, the Stop is never seen and the sensor stays on until a
later complete pair or a restart of Home Assistant. `diagnostics.py` has
reported the symptom for a while without anything acting on it: an event age of
21600 seconds is a binary sensor that has been on for six hours.

The fix asks the device, on reconnect, which events it still considers active,
and closes the ones it does not. That is better than clearing everything, which
is what Home Assistant's own ONVIF integration does on a lost subscription
(`async_mark_events_stale`), because asking is evidence and assuming is not.

**The thing that nearly made this unsafe**, measured on a DHI-NVR5464-16P-EI:

    code=VideoMotion          200  channels[0]=14
    code=CrossLineDetection   200  Error: No Events
    code=NotARealCodeAtAll    200  Error: No Events

A real code with nothing active and a code the endpoint knows nothing about
return the **same thing**. So an empty answer is not evidence an event ended,
and clearing on it would switch off a live sensor for any code the event stream
reports and this endpoint does not. That is the opposite of the bug and worse:
a security sensor reading clear while something is happening.

So the codes this endpoint can be trusted about are learned from the device, the
same way `event_is_momentary` learns what a Pulse is. The tests below are mostly
about that, because it is the part that can do harm.
"""

import ast
import io
import pathlib
from types import SimpleNamespace

import pytest

from custom_components.dahua import DahuaDataUpdateCoordinator, DahuaHostEventStream
from custom_components.dahua import host as host_module
from custom_components.dahua.host import (
    EVENT_RECONCILE_MAX_RECHECK_SECONDS,
    EVENT_RECONCILE_RECHECK_SECONDS,
    EVENT_STREAM_MAX_LIFETIME_SECONDS,
    unproven_latch_recheck_delay,
)


def _coordinator(channel, timestamps, momentary=()):
    """A coordinator with only what latched_events reads."""
    c = object.__new__(DahuaDataUpdateCoordinator)
    c._channel = channel
    c._dahua_event_timestamp = dict(timestamps)
    c.event_is_momentary = lambda code: code in momentary
    return c


class _Client:
    """A device that answers getEventIndexes, or will not."""

    def __init__(self, answers):
        self.answers = answers
        self.asked = []

    async def async_get_event_indexes_cgi(self, code):
        self.asked.append(code)
        answer = self.answers.get(code, set())
        if isinstance(answer, Exception):
            raise answer
        return answer


def _stream(coordinators, answers, learned=None):
    stream = object.__new__(DahuaHostEventStream)
    stream._address = "10.0.0.1"
    # `coordinators` is a read-only property over _by_channel, so the fake has
    # to populate what the property reads rather than assign the property. The
    # scratch harness I wrote this against used a plain object and assigned it
    # happily, which is exactly how a fake drifts from the thing it stands in
    # for.
    stream._by_channel = {
        coordinator._channel: [coordinator] for coordinator in coordinators
    }
    stream._owner = SimpleNamespace(client=_Client(answers))
    stream.dispatched = []
    stream._dispatch_events = lambda data, video_motion_source=None: (
        stream.dispatched.append(data.decode())
    )
    if learned is not None:
        stream._codes_the_device_reports = set(learned)
    return stream


# --- what counts as latched -------------------------------------------------


def test_an_event_showing_as_on_is_latched():
    coordinator = _coordinator(2, {"VideoMotion-2": 1760000000})

    assert coordinator.latched_events() == [("VideoMotion", 2)]


def test_an_event_that_was_stopped_is_not_latched():
    coordinator = _coordinator(2, {"VideoMotion-2": 0})

    assert coordinator.latched_events() == []


def test_a_momentary_event_is_not_latched():
    """A Pulse has no Stop coming and the sensor already expires it. Asking the
    device about one would be a request with nothing to decide."""
    coordinator = _coordinator(
        2, {"DoorbellPressed-2": 1760000000}, momentary=("DoorbellPressed",)
    )

    assert coordinator.latched_events() == []


def test_another_channels_event_is_not_this_channels():
    """The timestamps are keyed `<code>-<channel>`, and a coordinator answers
    only for its own channel."""
    coordinator = _coordinator(
        2, {"VideoMotion-2": 1760000000, "VideoMotion-7": 1760000000}
    )

    assert coordinator.latched_events() == [("VideoMotion", 2)]


def test_a_code_with_a_hyphen_in_it_survives_the_split():
    """The channel suffix is stripped by length, not by splitting on the first
    hyphen, so a code containing one is not truncated."""
    coordinator = _coordinator(3, {"IVSRule_a-b-3": 1760000000})

    assert coordinator.latched_events() == [("IVSRule_a-b", 3)]


# --- the ordinary case costs nothing ----------------------------------------


async def test_nothing_latched_asks_the_device_nothing():
    """This runs on every reconnect, and a failing stream reconnects often. It
    must not add load to a device that is already in trouble."""
    stream = _stream([_coordinator(0, {})], {})

    await stream._async_reconcile_latched_events()

    assert stream._owner.client.asked == []
    assert stream.dispatched == []


# --- the dangerous direction ------------------------------------------------


async def test_an_empty_answer_for_an_unproven_code_changes_nothing():
    """The measurement in the module docstring. `Error: No Events` arrives for a
    code with nothing active *and* for a code this endpoint knows nothing about,
    so an empty answer cannot be read as "the event ended"."""
    stream = _stream(
        [_coordinator(0, {"CrossLineDetection-0": 1760000000})],
        {"CrossLineDetection": set()},
    )

    await stream._async_reconcile_latched_events()

    assert stream.dispatched == [], "it closed an event on an ambiguous answer"


async def test_a_device_that_will_not_answer_changes_nothing():
    stream = _stream(
        [_coordinator(2, {"VideoMotion-2": 1760000000})],
        {"VideoMotion": OSError("device is down")},
        learned={"VideoMotion"},
    )

    await stream._async_reconcile_latched_events()

    assert stream.dispatched == []


async def test_an_event_the_device_still_reports_is_left_alone():
    stream = _stream(
        [_coordinator(2, {"VideoMotion-2": 1760000000})], {"VideoMotion": {2}}
    )

    await stream._async_reconcile_latched_events()

    assert stream.dispatched == []
    assert "VideoMotion" in stream._codes_the_device_reports


# --- and the fix ------------------------------------------------------------


async def test_a_lost_stop_is_closed_once_the_code_is_proven():
    stream = _stream(
        [_coordinator(2, {"VideoMotion-2": 1760000000})],
        {"VideoMotion": set()},
        learned={"VideoMotion"},
    )

    await stream._async_reconcile_latched_events()

    assert stream.dispatched == ["Code=VideoMotion;action=Stop;index=2\r\n"]


async def test_active_now_teaches_and_inactive_later_closes():
    """The order the learning happens in, which is what makes it work at all.

    While the event is genuinely on, the reconcile sees it reported, records the
    code, and clears nothing. The lost Stop after that can then be closed.
    """
    coordinator = _coordinator(2, {"VideoMotion-2": 1760000000})
    stream = _stream([coordinator], {"VideoMotion": {2}})

    await stream._async_reconcile_latched_events()
    taught = list(stream.dispatched)

    stream._owner.client.answers["VideoMotion"] = set()
    await stream._async_reconcile_latched_events()

    assert taught == [], "it closed the event while the device said it was active"
    assert stream.dispatched == ["Code=VideoMotion;action=Stop;index=2\r\n"]


async def test_only_the_channel_the_device_dropped_is_closed():
    stream = _stream(
        [
            _coordinator(1, {"VideoMotion-1": 1760000000}),
            _coordinator(3, {"VideoMotion-3": 1760000000}),
        ],
        {"VideoMotion": {1}},
        learned={"VideoMotion"},
    )

    await stream._async_reconcile_latched_events()

    assert stream.dispatched == ["Code=VideoMotion;action=Stop;index=3\r\n"]


async def test_one_hosts_learning_does_not_leak_into_another():
    """`_codes_the_device_reports` defaults to None on the class rather than to
    an empty set, because a mutable class attribute is shared by every
    instance -- and one recorder's endpoint behaviour says nothing about
    another's."""
    first = _stream(
        [_coordinator(0, {"VideoMotion-0": 1760000000})], {"VideoMotion": {0}}
    )
    await first._async_reconcile_latched_events()

    second = _stream(
        [_coordinator(0, {"VideoMotion-0": 1760000000})], {"VideoMotion": set()}
    )
    await second._async_reconcile_latched_events()

    assert second.dispatched == [], "it closed an event using another host's knowledge"


# --- and that it is actually wired in ---------------------------------------


def test_the_stream_loop_calls_the_reconcile():
    """Every test above calls the method directly, so deleting the one line that
    runs it would leave all of them green and the bug back. Checked against the
    source, because that is the only thing that can see the wiring.
    """
    source = pathlib.Path(__file__).resolve().parents[2] / "custom_components" / "dahua"
    tree = ast.parse(io.open(source / "host.py", encoding="utf-8").read())
    run = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_async_run"
    )
    calls = {
        ast.unparse(node.func) for node in ast.walk(run) if isinstance(node, ast.Call)
    }

    assert "self._async_reconcile_latched_events" in calls, (
        "_async_run no longer reconciles latched events, so a Stop lost while "
        "the stream was down stays lost and its sensor stays on. If the call "
        "moved, point this at where it went rather than deleting it."
    )


# --- one latch, one question (#1235) -----------------------------------------


class _Clock:
    def __init__(self):
        self.now = 5000.0

    def monotonic(self):
        return self.now


@pytest.fixture
def clock(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(host_module, "time", clock)
    return clock


def test_the_recheck_waits_five_minutes_then_doubles_to_an_hour():
    assert EVENT_RECONCILE_RECHECK_SECONDS == 300
    assert EVENT_RECONCILE_MAX_RECHECK_SECONDS == 3600
    assert [unproven_latch_recheck_delay(n) for n in range(1, 8)] == [
        300,
        600,
        1200,
        2400,
        3600,
        3600,
        3600,
    ]
    assert unproven_latch_recheck_delay(10_000) == 3600
    assert EVENT_RECONCILE_MAX_RECHECK_SECONDS <= EVENT_STREAM_MAX_LIFETIME_SECONDS, (
        "the stream recycles this often, so a latch is asked about on at "
        "least every recycle"
    )


async def test_the_same_unproven_latch_is_asked_about_once_inside_the_wait(clock):
    stream = _stream(
        [_coordinator(0, {"CrossLineDetection-0": 1760000000})],
        {"CrossLineDetection": set()},
    )

    for _ in range(7):
        await stream._async_reconcile_latched_events()
        clock.now += 40
    assert stream._owner.client.asked == ["CrossLineDetection"]

    clock.now += 40
    await stream._async_reconcile_latched_events()

    assert stream._owner.client.asked == ["CrossLineDetection"] * 2
    assert stream.dispatched == []


async def test_three_hours_of_reconnects_ask_six_times(clock):
    """#1235: a camera dropping its stream every 30 seconds kept one lost
    CrossLineDetection Stop latched, and every reconnect, about 40 seconds
    apart, asked the device about it again: 238 times in under three hours."""
    stream = _stream(
        [_coordinator(0, {"CrossLineDetection-0": 1760000000})],
        {"CrossLineDetection": set()},
    )
    asked_at = []
    for _ in range(270):
        before = len(stream._owner.client.asked)
        await stream._async_reconcile_latched_events()
        if len(stream._owner.client.asked) > before:
            asked_at.append(clock.now - 5000.0)
        clock.now += 40

    assert asked_at == [0, 320, 920, 2120, 4520, 8120]
    assert stream.dispatched == []


async def test_empty_then_active_then_empty_closes_it_with_no_start_pushed(clock):
    """rroller's review of #1238. The Stop is lost; the device says nothing
    is on, and has never reported the code, so nothing is closed. The event
    then comes on again with its Start missed, and off again with that Stop
    missed too. Asking once and never again left the sensor on for ever."""
    stream = _stream(
        [_coordinator(0, {"CrossLineDetection-0": 1760000000})],
        {"CrossLineDetection": set()},
    )
    await stream._async_reconcile_latched_events()
    assert stream.dispatched == []

    clock.now += EVENT_RECONCILE_RECHECK_SECONDS
    stream._owner.client.answers["CrossLineDetection"] = {0}
    await stream._async_reconcile_latched_events()
    assert stream._codes_the_device_reports == {"CrossLineDetection"}
    assert stream.dispatched == []

    clock.now += 40
    stream._owner.client.answers["CrossLineDetection"] = set()
    await stream._async_reconcile_latched_events()

    assert stream._owner.client.asked == ["CrossLineDetection"] * 3
    assert stream.dispatched == ["Code=CrossLineDetection;action=Stop;index=0\r\n"]


async def test_the_wait_doubles_for_the_same_latch(clock):
    stream = _stream(
        [_coordinator(0, {"CrossLineDetection-0": 1760000000})],
        {"CrossLineDetection": set()},
    )
    await stream._async_reconcile_latched_events()

    for wait in (300, 600, 1200, 2400, 3600, 3600):
        clock.now += wait - 1
        await stream._async_reconcile_latched_events()
        asked = len(stream._owner.client.asked)
        clock.now += 1
        await stream._async_reconcile_latched_events()
        assert len(stream._owner.client.asked) == asked + 1, wait


async def test_a_code_proven_inside_the_wait_is_not_made_to_wait(clock):
    """The wait belongs to a code the device has never reported. Here a second
    channel latches the same code inside channel 0's wait, the device reports
    it, and that proves the code. Channel 0's latch is then the one that was
    remembered, with its wait still running, and it must be asked about anyway.

    The fake does not clear a latch when its Stop is dispatched, which is what
    leaves channel 0 latched here. In the integration the first Stop clears it,
    so this pins the rule and not a sequence a device has been seen to produce.
    """
    first = _coordinator(0, {"CrossLineDetection-0": 1760000000})
    second = _coordinator(1, {})
    stream = _stream([first, second], {"CrossLineDetection": set()})
    await stream._async_reconcile_latched_events()

    clock.now += 40
    second._dahua_event_timestamp["CrossLineDetection-1"] = 1760000040
    stream._owner.client.answers["CrossLineDetection"] = {1}
    await stream._async_reconcile_latched_events()
    assert stream._codes_the_device_reports == {"CrossLineDetection"}

    clock.now += 40
    del second._dahua_event_timestamp["CrossLineDetection-1"]
    stream._owner.client.answers["CrossLineDetection"] = set()
    await stream._async_reconcile_latched_events()

    assert stream._owner.client.asked == ["CrossLineDetection"] * 3


async def test_a_new_start_does_not_inherit_the_old_latchs_wait(clock):
    coordinator = _coordinator(0, {"CrossLineDetection-0": 1760000000})
    stream = _stream([coordinator], {"CrossLineDetection": set()})
    for _ in range(4):
        await stream._async_reconcile_latched_events()
        clock.now += 3600

    coordinator._dahua_event_timestamp["CrossLineDetection-0"] = 1760000099
    await stream._async_reconcile_latched_events()
    asked = len(stream._owner.client.asked)
    clock.now += EVENT_RECONCILE_RECHECK_SECONDS
    await stream._async_reconcile_latched_events()

    assert len(stream._owner.client.asked) == asked + 1


async def test_a_new_start_is_a_new_latch_and_is_asked_about(clock):
    coordinator = _coordinator(0, {"CrossLineDetection-0": 1760000000})
    stream = _stream([coordinator], {"CrossLineDetection": set()})
    await stream._async_reconcile_latched_events()
    await stream._async_reconcile_latched_events()

    coordinator._dahua_event_timestamp["CrossLineDetection-0"] = 1760000099
    await stream._async_reconcile_latched_events()

    assert stream._owner.client.asked == ["CrossLineDetection"] * 2


async def test_a_new_channel_latching_the_same_code_is_asked_about(clock):
    first = _coordinator(0, {"CrossLineDetection-0": 1760000000})
    second = _coordinator(1, {})
    stream = _stream([first, second], {"CrossLineDetection": set()})
    await stream._async_reconcile_latched_events()

    second._dahua_event_timestamp["CrossLineDetection-1"] = 1760000050
    await stream._async_reconcile_latched_events()

    assert stream._owner.client.asked == ["CrossLineDetection"] * 2


async def test_a_proven_code_is_still_asked_about_every_time():
    """The memo is only for answers that cannot matter. A code the endpoint
    reports is the one whose lost Stop can be closed, so it keeps being asked:
    the event may have ended between two reconnects."""
    stream = _stream(
        [_coordinator(2, {"VideoMotion-2": 1760000000})],
        {"VideoMotion": {2}},
        learned={"VideoMotion"},
    )
    await stream._async_reconcile_latched_events()
    await stream._async_reconcile_latched_events()
    stream._owner.client.answers["VideoMotion"] = set()
    await stream._async_reconcile_latched_events()

    assert stream._owner.client.asked == ["VideoMotion"] * 3
    assert stream.dispatched == ["Code=VideoMotion;action=Stop;index=2\r\n"]


async def test_a_device_that_did_not_answer_is_asked_again(clock):
    """A failed request learned nothing, so it is not remembered as asked."""
    stream = _stream(
        [_coordinator(0, {"CrossLineDetection-0": 1760000000})],
        {"CrossLineDetection": OSError("device is down")},
    )
    await stream._async_reconcile_latched_events()
    stream._owner.client.answers["CrossLineDetection"] = set()
    await stream._async_reconcile_latched_events()
    await stream._async_reconcile_latched_events()

    assert stream._owner.client.asked == ["CrossLineDetection"] * 2


async def test_one_hosts_memo_does_not_leak_into_another(clock):
    first = _stream(
        [_coordinator(0, {"CrossLineDetection-0": 1760000000})],
        {"CrossLineDetection": set()},
    )
    await first._async_reconcile_latched_events()

    second = _stream(
        [_coordinator(0, {"CrossLineDetection-0": 1760000000})],
        {"CrossLineDetection": set()},
    )
    await second._async_reconcile_latched_events()

    assert second._owner.client.asked == ["CrossLineDetection"]
