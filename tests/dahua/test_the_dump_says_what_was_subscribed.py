"""Two fields in the diagnostics dump that could not answer the question asked of them.

Both are about the same stretch of code: an event leaves the device, and somewhere
between the wire and a binary sensor it stops mattering. #825 has spent days trying to
tell four cases apart from the outside, and the dump could distinguish three.

**What was subscribed.** `stream.attached_events` reported the union of the channels'
selections and described itself as what the device was asked to send. It is not always
that. Sharing one stream per host makes the request the union across channels, and some
firmware answers `codes=[All]` but goes silent on a long explicit list, so when the
union is wider than any single channel's own list the stream sends `[All]` and filters
again locally. In exactly that configuration the field named a request the device never
received. `subscribed_as` is the wire request; `attached_events` keeps its real meaning,
which is the selection.

**An event that updates nothing.** `_dispatch_event` looks up `"<Code>-<channel>"` and
continues when no listener is registered. The timestamp a binary sensor reads is written
*after* that check, so a sensor whose key is absent can never move, and nothing says so:
the event was already put on the Home Assistant event bus by then. From outside, a
reporter watching the bus sees events arriving and a sensor that stays off, which looks
identical to a device that stopped sending. `events.arrived_with_no_listener` counts
them by key.
"""

from types import SimpleNamespace

import pytest

import custom_components.dahua as dahua
from custom_components.dahua import DahuaDataUpdateCoordinator
from custom_components.dahua.diagnostics import _events_block, _stream_block

ADDRESS = "10.0.0.94"

UNION = frozenset({"VideoMotion", "AlarmLocal", "CrossRegionDetection"})


class _Task:
    def done(self):
        return False


def _stream(**kwargs):
    """A stand-in for DahuaHostEventStream, with its real attribute names."""
    defaults = {
        "_task": _Task(),
        "_events": UNION,
        "_using_all_events": False,
        "_received_data": True,
        "_failing": False,
        "_consecutive_failures": 0,
        "_owner": None,
        "_by_channel": {},
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _reader(address=ADDRESS, **kwargs):
    """The shape _stream_block and _events_block read, and nothing more."""
    defaults = {
        "_address": address,
        "_event_task": None,
        "_vto_task": None,
        "_vto_client": None,
        "_dahua_event_listeners": {},
        "_dahua_event_timestamp": {},
        "_recent_events": [],
        "get_event_list": lambda: ["VideoMotion"],
        "get_channel": lambda: 0,
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


@pytest.fixture(autouse=True)
def _clean_streams():
    dahua._HOST_STREAMS.clear()
    yield
    dahua._HOST_STREAMS.clear()


# --- what the device was actually asked for ---------------------------------

def test_an_explicit_subscription_is_reported_as_itself():
    """The ordinary case, and the one every single camera takes."""
    dahua._HOST_STREAMS[ADDRESS] = _stream(_using_all_events=False)

    block = _stream_block(_reader())

    assert block["subscribed_as"] == [
        "AlarmLocal", "CrossRegionDetection", "VideoMotion"]
    assert block["subscribed_as"] == block["attached_events"]


def test_a_broadened_subscription_is_reported_as_All():
    """The case the old field described wrongly. The device received `codes=[All]`
    and the dump said it received three named codes, so a reporter comparing the
    dump against a packet capture would have found the dump wrong and had no way
    to know which half to trust."""
    dahua._HOST_STREAMS[ADDRESS] = _stream(_using_all_events=True)

    block = _stream_block(_reader())

    assert block["subscribed_as"] == ["All"]


def test_the_selection_is_still_reported_when_All_was_sent():
    """Both, not one. `[All]` on the wire and a three code selection locally is the
    configuration where events arrive and are then filtered out again in
    `on_receive`, and only having both fields makes that legible."""
    dahua._HOST_STREAMS[ADDRESS] = _stream(_using_all_events=True)

    block = _stream_block(_reader())

    assert block["attached_events"] == [
        "AlarmLocal", "CrossRegionDetection", "VideoMotion"]
    assert block["subscribed_as"] == ["All"]


def test_a_stream_that_never_set_the_flag_reads_as_explicit():
    """Read with a default rather than assumed present. The real class does declare
    `_using_all_events = False`, so this is about the tests and fakes that build a
    stream by hand: a missing attribute must not make the dump raise, and the old
    behaviour is the right answer for a stream that never broadened anything."""
    bare = _stream()
    del bare._using_all_events
    dahua._HOST_STREAMS[ADDRESS] = bare

    assert _stream_block(_reader())["subscribed_as"] == [
        "AlarmLocal", "CrossRegionDetection", "VideoMotion"]


# --- an event that arrived and moved nothing --------------------------------

def _coordinator(channel=0):
    """A coordinator built the way this suite builds them, exercising the real
    `_dispatch_event` rather than a description of it."""
    c = object.__new__(DahuaDataUpdateCoordinator)
    c._channel = channel
    c._dahua_event_timestamp = {}
    c._dahua_event_listeners = {}
    c._address = ADDRESS
    c.fired = []
    c.hass = SimpleNamespace(
        bus=SimpleNamespace(fire=lambda *a, **k: None),
        async_create_task=lambda coro: coro.close(),
    )
    c.get_device_name = lambda: "Side Gate"
    c._handle_anpr_plate = lambda event: None
    return c


def test_an_event_with_no_listener_is_counted():
    c = _coordinator()

    c._dispatch_event({"Code": "SmartMotionHuman", "action": "Start"}, "Start")

    assert c._events_without_listener == {"SmartMotionHuman-0": 1}


def test_the_count_is_per_key_and_accumulates():
    """Once per arrival, so "it happened" and "it is happening constantly" read
    differently. A sensor that was removed once is not the same report as a code
    the device sends every few seconds to nobody."""
    c = _coordinator()

    for _ in range(3):
        c._dispatch_event({"Code": "FaceDetection", "action": "Start"}, "Start")
    c._dispatch_event({"Code": "SmartMotionVehicle", "action": "Start"}, "Start")

    assert c._events_without_listener == {
        "FaceDetection-0": 3, "SmartMotionVehicle-0": 1}


def test_the_key_carries_the_channel():
    """Which is the whole point on a recorder: the same code arriving for a channel
    that has a sensor and a channel that does not."""
    c = _coordinator(channel=7)

    c._dispatch_event({"Code": "VideoMotion", "action": "Start"}, "Start")

    assert c._events_without_listener == {"VideoMotion-7": 1}


def test_an_event_that_reaches_its_sensor_is_not_counted():
    """The negative control. Without it this would pass just as well if every
    event were counted, which would make the field meaningless."""
    c = _coordinator()
    c.add_dahua_event_listener("VideoMotion", lambda: c.fired.append(1))

    c._dispatch_event({"Code": "VideoMotion", "action": "Start"}, "Start")

    assert c.fired == [1]
    assert not c._events_without_listener


def test_two_coordinators_do_not_pool_their_counts():
    """The counter starts as a class attribute so that the many tests building a
    coordinator with `object.__new__` find something there. A dict would have been
    one dict for every coordinator in the process, so eleven channels of a recorder
    would report each other's misses and the channel column would be a lie."""
    one = _coordinator(channel=0)
    two = _coordinator(channel=1)

    one._dispatch_event({"Code": "VideoMotion", "action": "Start"}, "Start")

    assert one._events_without_listener == {"VideoMotion-0": 1}
    assert not two._events_without_listener


# --- and it reaches the dump ------------------------------------------------

def test_the_counts_reach_the_diagnostics_dump():
    dahua._HOST_STREAMS[ADDRESS] = _stream()

    block = _events_block(_reader(
        _events_without_listener={"FaceDetection-0": 4}))

    assert block["arrived_with_no_listener"] == {"FaceDetection-0": 4}


def test_a_coordinator_that_has_missed_nothing_reports_an_empty_map():
    """Empty rather than absent, so the field being there is not itself a signal."""
    dahua._HOST_STREAMS[ADDRESS] = _stream()

    block = _events_block(_reader())

    assert block["arrived_with_no_listener"] == {}


def test_the_dump_copies_the_counts_rather_than_publishing_the_live_dict():
    """Diagnostics runs while the stream keeps dispatching. Handing out the live
    dict would let it change under serialisation, which is how a dump ends up
    raising instead of downloading."""
    dahua._HOST_STREAMS[ADDRESS] = _stream()
    live = {"FaceDetection-0": 1}

    block = _events_block(_reader(_events_without_listener=live))
    live["FaceDetection-0"] = 99

    assert block["arrived_with_no_listener"] == {"FaceDetection-0": 1}
