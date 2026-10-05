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


@pytest.fixture(autouse=True)
async def _stop_host_streams(hass):
    """Cancel and await each host stream's task before Home Assistant looks.

    Two things make this necessary. cancel() only schedules the cancellation,
    so a merely-cancelled task is still pending, and Home Assistant fails a
    test that leaves one behind. And asking for `hass` is what gets the timing
    right: a fixture that requests another is torn down before it, so this runs
    while there is still a loop to finish the task on.
    """
    yield
    from custom_components import dahua as dahua_module

    pending = []
    for stream in list(dahua_module._HOST_STREAMS.values()):
        task = getattr(stream, "_task", None)
        if task is not None and not task.done():
            task.cancel()
            pending.append(task)
    dahua_module._HOST_STREAMS.clear()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


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
        assert (
            channels[i].handled == []
        ), f"channel {i} received another channel's event"


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

    stream.on_receive(ALARM_CH1, 0)  # must not raise

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
    assert (
        ch0.handled == []
    ), "AlarmLocal leaked to a channel that isn't the terminal's index"


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


async def test_expanded_shared_subscription_attaches_with_all_events(hass):
    """Use All only when sharing makes the event list broader than before #615."""
    stream = _host_stream(hass, ADDRESS)
    first = _Coordinator(0, ["VideoMotion"])
    second = _Coordinator(1, ["CrossLineDetection", "AlarmLocal"])
    stream.register(first)
    stream.register(second)
    await _settle()

    assert first.client.attached_with[-1] == ["All"]


async def test_shared_subscription_stays_explicit_when_union_does_not_grow(hass):
    """Two channels choosing the same event do not need the All fallback."""
    stream = _host_stream(hass, ADDRESS)
    first = _Coordinator(0, ["VideoMotion"])
    second = _Coordinator(1, ["VideoMotion"])
    stream.register(first)
    await _settle()
    before = first.client.attach_count

    stream.register(second)
    await _settle()

    assert first.client.attach_count == before
    assert first.client.attached_with[-1] == ["VideoMotion"]


async def test_single_channel_keeps_its_explicit_event_subscription(hass):
    """Single-camera behaviour is unchanged by the NVR compatibility path."""
    stream = _host_stream(hass, ADDRESS)
    only = _Coordinator(0, ["VideoMotion", "AlarmLocal"])
    stream.register(only)
    await _settle()

    assert set(only.client.attached_with[-1]) == {"VideoMotion", "AlarmLocal"}


async def test_all_subscription_still_filters_unrequested_codes(hass):
    """codes=[All] on the wire must not broaden Home Assistant dispatch."""
    stream = _host_stream(hass, ADDRESS)
    first = _Coordinator(0, ["VideoMotion"])
    second = _Coordinator(1, ["AlarmLocal"])
    stream.register(first)
    stream.register(second)
    await _settle()

    stream.on_receive(
        b"Code=NewFile;action=Start;index=0\r\n",
        0,
    )

    assert first.handled == []
    assert second.handled == []


async def test_derived_smart_motion_allows_raw_ivs_event_through(hass):
    """SmartMotionHuman may be selected even though the wire code is CrossRegionDetection."""
    stream = _host_stream(hass, ADDRESS)
    first = _Coordinator(0, ["SmartMotionHuman"])
    second = _Coordinator(1, ["VideoMotion"])
    stream.register(first)
    stream.register(second)
    await _settle()

    stream.on_receive(
        (
            b"Code=CrossRegionDetection;action=Start;index=0;"
            b'data={"Object":{"ObjectType":"Human"}}\r\n'
        ),
        0,
    )

    assert len(first.handled) == 1
    assert first.handled[0]["Code"] == "CrossRegionDetection"
    assert second.handled == []


async def test_it_does_not_re_attach_when_nothing_new_is_wanted(hass):
    stream = _host_stream(hass, ADDRESS)
    first = _Coordinator(0, ["VideoMotion"])
    stream.register(first)
    await _settle()
    before = first.client.attach_count

    stream.register(_Coordinator(1, ["VideoMotion"]))
    await _settle()

    assert first.client.attach_count == before, "re-attached for no reason"


async def test_it_re_attaches_with_all_when_union_expands(hass):
    stream = _host_stream(hass, ADDRESS)
    first = _Coordinator(0, ["VideoMotion"])
    stream.register(first)
    await _settle()

    stream.register(_Coordinator(1, ["FaceDetection"]))
    await _settle()

    assert first.client.attached_with[-1] == ["All"]


async def test_a_channel_with_no_events_starts_nothing(hass):
    stream = DahuaHostEventStream(hass, ADDRESS)
    stream.register(_Coordinator(0, []))
    await _settle()

    assert stream._task is None


# --- what the All fallback must not cost --------------------------------------


async def test_each_channel_still_gets_its_own_codes_under_all(hass):
    """The guarantee the pre-#615 union attach used to hold, restated for `All`.

    `test_expanded_shared_subscription_attaches_with_all_events` proves the wire
    subscription broadens, and `test_all_subscription_still_filters_unrequested_codes`
    proves an unwanted code is dropped. Neither proves the thing the original test
    was for: that after all that, each channel still receives what it asked for.
    Without this, a filter that dropped everything would pass the suite.
    """
    stream = _host_stream(hass, ADDRESS)
    first = _Coordinator(0, ["VideoMotion"])
    second = _Coordinator(1, ["AlarmLocal"])
    stream.register(first)
    stream.register(second)
    await _settle()

    assert first.client.attached_with[-1] == ["All"], "not the All path"

    stream.on_receive(b"Code=VideoMotion;action=Start;index=0\r\n", 0)
    stream.on_receive(b"Code=AlarmLocal;action=Start;index=1\r\n", 1)

    assert [event["Code"] for event in first.handled] == ["VideoMotion"]
    assert [event["Code"] for event in second.handled] == ["AlarmLocal"]


async def test_a_channels_own_code_is_not_dropped_because_a_sibling_wanted_it(hass):
    """The union is what the filter checks, so a code only one channel selected
    must still reach that channel and nobody else."""
    stream = _host_stream(hass, ADDRESS)
    first = _Coordinator(0, ["VideoMotion", "AlarmLocal"])
    second = _Coordinator(1, ["VideoMotion"])
    stream.register(first)
    stream.register(second)
    await _settle()

    stream.on_receive(b"Code=AlarmLocal;action=Start;index=0\r\n", 0)

    assert [event["Code"] for event in first.handled] == ["AlarmLocal"]
    assert second.handled == []


# --- and the coupling that keeps DERIVES_INTO honest --------------------------


def test_a_translated_code_that_becomes_selectable_must_be_mapped():
    """`translate_event_code` rewrites four raw codes, and the host filter runs
    before it, so any rewrite whose *target* a user can select needs its source
    in DERIVES_INTO or the event is dropped before translation.

    Two are mapped. The doorbell pair is safe only because `DoorbellPressed` is
    absent from ALL_EVENTS and so can never be in anybody's selection. That is a
    fact about the selectable list rather than about doorbells, and it would stop
    being true the moment somebody made it selectable, which is exactly the
    change this catches. #556 and #593 were both about `BackKeyLight` reaching an
    entity, so it is not a hypothetical request.
    """
    from custom_components.dahua.config_flow import ALL_EVENTS
    from custom_components.dahua import DERIVES_INTO

    translations = {
        "CrossLineDetection": ("SmartMotionHuman", "SmartMotionVehicle"),
        "CrossRegionDetection": ("SmartMotionHuman", "SmartMotionVehicle"),
        "BackKeyLight": ("DoorbellPressed",),
        "PhoneCallDetect": ("DoorbellPressed",),
    }

    for raw, derived in translations.items():
        selectable = [code for code in derived if code in ALL_EVENTS]
        if not selectable:
            continue
        assert raw in DERIVES_INTO, (
            "%s is selectable and is derived from %s, which the host filter "
            "drops before translation. Add it to DERIVES_INTO." % (selectable, raw)
        )
        assert set(selectable) <= set(DERIVES_INTO[raw])


def test_the_mapped_sources_are_codes_a_device_actually_sends():
    """A map keyed on something unselectable would silently never fire. Both
    keys are real IVS codes a user can also select in their own right."""
    from custom_components.dahua.config_flow import ALL_EVENTS
    from custom_components.dahua import DERIVES_INTO

    assert set(DERIVES_INTO) <= set(ALL_EVENTS)


# --- the flag has to survive an incompletely built stream ---------------------


def test_the_broadened_flag_has_a_class_level_default():
    """Both `_async_run` and `on_receive` read `_using_all_events`, and several
    test files build this object with `object.__new__` and set only what they are
    about: `test_one_bad_event.py` sets three attributes, `test_refused_
    credentials.py` six, and neither is about the subscription shape.

    Assigning it only in `__init__` was a real CI failure rather than a
    hypothetical one. `test_the_event_stream_stops_once_the_budget_is_gone` calls
    `_async_run` directly on a bare instance, and the AttributeError landed inside
    that method's retry loop, so it reported a nine second timeout rather than an
    error and said nothing about the cause.
    """
    bare = DahuaHostEventStream.__new__(DahuaHostEventStream)

    assert bare._using_all_events is False


def test_a_bare_stream_dispatches_as_it_did_before(hass):
    """The consequence of that default, stated as behaviour rather than as an
    attribute: a stream with no `_events` at all still delivers, because it was
    never broadened and so is not filtered."""
    bare = DahuaHostEventStream.__new__(DahuaHostEventStream)
    bare._address = ADDRESS
    bare._received_data = False
    only = _Coordinator(0, ["VideoMotion"])
    bare._by_channel = {0: [only]}

    bare.on_receive(b"Code=VideoMotion;action=Start;index=0\r\n", 0)

    assert [event["Code"] for event in only.handled] == ["VideoMotion"]


# --- an index the device did not send as a number ----------------------------

MOTION_BAD_INDEX = (
    b"--myboundary\n"
    b"Content-Type: text/plain\n"
    b"Content-Length: 44\n"
    b"\n"
    b"Code=VideoMotion;action=Start;index=Channel1\n"
)


async def test_an_index_that_is_not_a_number_falls_back_to_channel_zero(hass):
    """A device that puts something other than a number in `index` must not take the
    dispatch down with it. Everything on that stream shares this one call, so one
    malformed event would cost every channel of the host its events for that read.

    Zero rather than dropped, because an event with an index nobody can read is still an
    event, and channel 0 is the one every host has.
    """
    stream = _host_stream(hass, ADDRESS)
    on_zero = _Coordinator(0, ["VideoMotion"])
    stream.register(on_zero)
    await _settle()

    stream.on_receive(MOTION_BAD_INDEX, 0)

    assert on_zero.handled, "a non-numeric index cost channel 0 its event"


# One read can carry several events, so the fallback has to be per event rather
# than per read: the bad one must not take its neighbours with it.
BAD_INDEX_THEN_GOOD = MOTION_BAD_INDEX + (
    b"--myboundary\n"
    b"Content-Type: text/plain\n"
    b"Content-Length: 39\n"
    b"\n"
    b"Code=CrossLineDetection;action=Start;index=0\n"
)


async def test_a_malformed_index_does_not_cost_the_next_event_in_the_same_read(hass):
    """One frame, two events, the first unreadable.

    The parse is shared across the whole host, so a per-read failure would be a
    host-wide outage triggered by whichever device sends the odd index.
    """
    stream = _host_stream(hass, ADDRESS)
    on_zero = _Coordinator(0, ["VideoMotion", "CrossLineDetection"])
    stream.register(on_zero)
    await _settle()

    stream.on_receive(BAD_INDEX_THEN_GOOD, 0)

    codes = [event["Code"] for event in on_zero.handled]
    assert codes == ["VideoMotion", "CrossLineDetection"], codes
