"""One NVR holds one event stream, not one per channel."""

import asyncio

import pytest

from custom_components import dahua as dahua_module
from custom_components.dahua import (
    DahuaHostEventStream,
    _host_stream,
    _release_host_stream,
)

ADDRESS = "10.0.0.1"

MOTION_CH2 = (
    b"--myboundary\n"
    b"Content-Type: text/plain\n"
    b"Content-Length: 40\n"
    b"\n"
    b"Code=VideoMotion;action=Start;index=2\n"
)


@pytest.fixture(autouse=True)
def _clean_streams():
    dahua_module._HOST_STREAMS.clear()
    yield
    dahua_module._HOST_STREAMS.clear()


class _Client:
    """Holds the stream open so the task stays alive, and records the attach."""

    def __init__(self):
        self.attached_with = []
        self.attach_count = 0
        self.closed = False

    async def stream_events(self, on_receive, events, channel):
        self.attach_count += 1
        self.attached_with.append(list(events))
        if self.closed:
            raise RuntimeError("session is closed")
        await asyncio.Event().wait()


class _Coordinator:
    def __init__(self, channel, events, client=None):
        self._channel = channel
        self.events = events
        self.client = client or _Client()
        self.handled = []

    def get_channel(self):
        return self._channel

    def get_address(self):
        return ADDRESS

    def handle_event(self, event):
        self.handled.append(event)


async def _settle():
    """Let the freshly created stream task reach its first await."""
    await asyncio.sleep(0)
    await asyncio.sleep(0)


# --- one stream, not eleven -------------------------------------------------

async def test_eleven_channels_share_one_stream(hass):
    stream = _host_stream(hass, ADDRESS)
    coordinators = [_Coordinator(i, ["VideoMotion"]) for i in range(11)]
    for coordinator in coordinators:
        stream.register(coordinator)
    await _settle()

    attaches = sum(c.client.attach_count for c in coordinators)
    assert attaches == 1, "the device is still seeing one stream per channel"
    assert len(dahua_module._HOST_STREAMS) == 1


async def test_two_hosts_keep_their_own_streams(hass):
    a = _host_stream(hass, ADDRESS)
    b = _host_stream(hass, "10.0.0.2")
    assert a is not b


async def test_a_trailing_slash_is_the_same_host(hass):
    assert _host_stream(hass, ADDRESS) is _host_stream(hass, ADDRESS + "/")


# --- the refcount case that would break an NVR -----------------------------

async def test_unloading_one_channel_leaves_the_others_streaming(hass):
    """Reloading a single channel calls unload then setup on that entry only.

    If that tore down the shared stream, the other ten channels would go deaf.
    """
    stream = _host_stream(hass, ADDRESS)
    kept = [_Coordinator(i, ["VideoMotion"]) for i in range(10)]
    leaving = _Coordinator(10, ["VideoMotion"])
    for coordinator in [*kept, leaving]:
        stream.register(coordinator)
    await _settle()

    emptied = await stream.unregister(leaving)
    await _settle()

    assert emptied is False
    assert stream._task is not None and not stream._task.done()
    stream.on_receive(MOTION_CH2, 0)
    assert kept[2].handled, "the survivors stopped receiving events"


async def test_the_last_channel_leaving_stops_the_stream(hass):
    stream = _host_stream(hass, ADDRESS)
    only = _Coordinator(0, ["VideoMotion"])
    stream.register(only)
    await _settle()
    task = stream._task

    await _release_host_stream(only)
    await _settle()

    assert task.cancelled() or task.done()
    assert ADDRESS not in dahua_module._HOST_STREAMS


async def test_the_stream_moves_off_a_departing_owner(hass):
    """The stream borrows a client, and unloading an entry closes its session."""
    stream = _host_stream(hass, ADDRESS)
    owner = _Coordinator(0, ["VideoMotion"])
    other = _Coordinator(1, ["VideoMotion"])
    stream.register(owner)
    stream.register(other)
    await _settle()

    owner.client.closed = True
    await stream.unregister(owner)
    await _settle()

    assert stream._owner is other
    assert other.client.attach_count >= 1, "did not re-attach on a live client"


# --- dispatch ---------------------------------------------------------------

async def test_an_event_reaches_only_its_own_channel(hass):
    stream = _host_stream(hass, ADDRESS)
    channels = [_Coordinator(i, ["VideoMotion"]) for i in range(4)]
    for coordinator in channels:
        stream.register(coordinator)
    await _settle()

    stream.on_receive(MOTION_CH2, 0)

    assert len(channels[2].handled) == 1
    assert channels[2].handled[0]["Code"] == "VideoMotion"
    for i in (0, 1, 3):
        assert channels[i].handled == [], f"channel {i} received another channel's event"


async def test_an_unconfigured_channel_stays_silent(hass):
    """Every coordinator used to discard these, so nothing fired. Keep it that way."""
    stream = _host_stream(hass, ADDRESS)
    only = _Coordinator(0, ["VideoMotion"])
    stream.register(only)
    await _settle()

    stream.on_receive(MOTION_CH2, 0)  # index 2, and nobody is on channel 2

    assert only.handled == []


async def test_two_entries_on_one_channel_both_get_it(hass):
    stream = _host_stream(hass, ADDRESS)
    a, b = _Coordinator(2, ["VideoMotion"]), _Coordinator(2, ["VideoMotion"])
    stream.register(a)
    stream.register(b)
    await _settle()

    stream.on_receive(MOTION_CH2, 0)

    assert len(a.handled) == 1 and len(b.handled) == 1


# --- AlarmLocal: index is a terminal, not a channel (#231) ------------------

ALARM_CH1 = (
    b"--myboundary\n"
    b"Content-Type: text/plain\n"
    b"Content-Length: 39\n"
    b"\n"
    b"Code=AlarmLocal;action=Start;index=1\n"
)


async def test_alarmlocal_reaches_the_lone_channel_regardless_of_index(hass):
    """The original #231 bug: an alarm on terminal 1 with only channel 0 set up."""
    stream = _host_stream(hass, ADDRESS)
    only = _Coordinator(0, ["AlarmLocal"])
    stream.register(only)
    await _settle()

    stream.on_receive(ALARM_CH1, 0)

    assert len(only.handled) == 1
    assert only.handled[0]["Code"] == "AlarmLocal"


async def test_alarmlocal_reaches_both_entries_sharing_the_lone_channel(hass):
    """Two entries on the same single channel must both still get it (see
    test_two_entries_on_one_channel_both_get_it) -- guarding on coordinator
    count instead of channel count would silently drop this case."""
    stream = _host_stream(hass, ADDRESS)
    a, b = _Coordinator(0, ["AlarmLocal"]), _Coordinator(0, ["AlarmLocal"])
    stream.register(a)
    stream.register(b)
    await _settle()

    stream.on_receive(ALARM_CH1, 0)

    assert len(a.handled) == 1 and len(b.handled) == 1


class _ExplodingCoordinator(_Coordinator):
    """A handler that raises, as a malformed payload can make one do."""

    def handle_event(self, event):
        self.handled.append(event)
        raise AttributeError("'str' object has no attribute 'get'")


async def test_a_failing_alarmlocal_handler_does_not_end_the_stream(hass):
    """stream_events wraps its on_receive call in try/finally with no handler.

    So anything raised while handling an event leaves the read loop -- and the
    stream is per host, so that silences every camera on the device until the
    backoff reconnects. The dispatch below is guarded for that reason (#706);
    this branch reaches handle_event through its own guard instead.
    """
    stream = _host_stream(hass, ADDRESS)
    boom = _ExplodingCoordinator(0, ["AlarmLocal"])
    stream.register(boom)
    await _settle()

    stream.on_receive(ALARM_CH1, 0)     # must not raise

    assert len(boom.handled) == 1, "the event still reached the handler"


async def test_a_non_alarmlocal_event_still_respects_the_lone_channel(hass):
    """The AlarmLocal early return must not widen what any other code does,
    even on a host with only one channel configured."""
    stream = _host_stream(hass, ADDRESS)
    only = _Coordinator(0, ["VideoMotion"])
    stream.register(only)
    await _settle()

    stream.on_receive(MOTION_CH2, 0)  # index 2, and nobody is on channel 2

    assert only.handled == []


async def test_alarmlocal_still_filtered_by_channel_on_a_multi_channel_host(hass):
    """With more than one channel configured, AlarmLocal is not exempt: this
    is what pins the len(self._by_channel) == 1 guard against a later
    refactor loosening it."""
    stream = _host_stream(hass, ADDRESS)
    ch0 = _Coordinator(0, ["AlarmLocal"])
    ch1 = _Coordinator(1, ["AlarmLocal"])
    stream.register(ch0)
    stream.register(ch1)
    await _settle()

    stream.on_receive(ALARM_CH1, 0)  # index=1

    assert len(ch1.handled) == 1
    assert ch0.handled == [], "AlarmLocal leaked to a channel that isn't the terminal's index"


async def test_each_channel_gets_its_own_copy(hass):
    """handle_event mutates the event, so channels must not share one dict."""
    stream = _host_stream(hass, ADDRESS)
    a, b = _Coordinator(2, ["VideoMotion"]), _Coordinator(2, ["VideoMotion"])
    stream.register(a)
    stream.register(b)
    await _settle()

    stream.on_receive(MOTION_CH2, 0)

    assert a.handled[0] is not b.handled[0]


async def test_junk_on_the_wire_is_ignored(hass):
    stream = _host_stream(hass, ADDRESS)
    only = _Coordinator(0, ["VideoMotion"])
    stream.register(only)
    await _settle()

    stream.on_receive(b"not an event at all", 0)

    assert only.handled == []


# --- the attach itself ------------------------------------------------------

async def test_the_stream_attaches_with_every_channels_events(hass):
    """Attaching with one channel's list would stop delivering another's codes."""
    stream = _host_stream(hass, ADDRESS)
    first = _Coordinator(0, ["VideoMotion"])
    second = _Coordinator(1, ["CrossLineDetection", "AlarmLocal"])
    stream.register(first)
    stream.register(second)
    await _settle()

    attached = first.client.attached_with[-1]
    assert set(attached) == {"VideoMotion", "CrossLineDetection", "AlarmLocal"}


async def test_it_does_not_re_attach_when_nothing_new_is_wanted(hass):
    stream = _host_stream(hass, ADDRESS)
    first = _Coordinator(0, ["VideoMotion"])
    stream.register(first)
    await _settle()
    before = first.client.attach_count

    stream.register(_Coordinator(1, ["VideoMotion"]))
    await _settle()

    assert first.client.attach_count == before, "re-attached for no reason"


async def test_it_re_attaches_when_a_channel_wants_something_new(hass):
    stream = _host_stream(hass, ADDRESS)
    first = _Coordinator(0, ["VideoMotion"])
    stream.register(first)
    await _settle()

    stream.register(_Coordinator(1, ["FaceDetection"]))
    await _settle()

    assert "FaceDetection" in first.client.attached_with[-1]


async def test_a_channel_with_no_events_starts_nothing(hass):
    stream = DahuaHostEventStream(hass, ADDRESS)
    stream.register(_Coordinator(0, []))
    await _settle()

    assert stream._task is None
