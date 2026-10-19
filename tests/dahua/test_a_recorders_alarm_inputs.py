"""A recorder's alarm terminals are the box's, not its cameras' (#1090).

The bug: `AlarmLocal`'s `index` is the position in the recorder's own `Alarm[]`
table, and `host.py` dispatched it through `_by_channel`, which treats an index
as a video channel. On the DHI-NVR5464-16P-EI this was measured directly, by
flipping unwired inputs from `SensorType: NO` to `NC` so an open circuit reads
as the alarm state:

    Alarm[3]   named "Alarm-in Port4"    ->  index=3   getInState result=8
    Alarm[14]  named "Alarm-in Port15"   ->  index=14  getInState result=16384

and both lit a camera's safety sensor in Home Assistant -- terminal 4 raised a
sensor belonging to a camera pointed at a rear lane, which has no alarm
terminal at all.

So the numbers in this file are the device's, not invented: the zero-based wire
index, the one-based label the device prints, and the bitmask bit positions.
"""

import asyncio
import time

import pytest

from custom_components import dahua as dahua_module
from custom_components.dahua import (
    DahuaHostEventStream,
    _host_stream,
    alarm_input_event_code,
    alarm_input_index,
)
from custom_components.dahua.client import (
    parse_alarm_inputs,
    parse_alarm_state_bitmask,
)
from custom_components.dahua.coordinator import DahuaDataUpdateCoordinator

ADDRESS = "10.0.0.7"

# Exactly what the recorder answered, trimmed to three terminals. The
# EventHandler subtree is kept on one of them because it is most of the real
# payload and must not leak into an entity.
ALARM_TABLE = {
    "table.Alarm[0].Enable": "true",
    "table.Alarm[0].Name": "Alarm-in Port1",
    "table.Alarm[0].SensorType": "NO",
    "table.Alarm[0].EventHandler.AlarmOutEnable": "false",
    "table.Alarm[0].EventHandler.BeepEnable": "false",
    "table.Alarm[0].EventHandler.AlarmOutChannels[0]": "0",
    "table.Alarm[1].Enable": "false",
    "table.Alarm[1].Name": "Alarm-in Port2",
    "table.Alarm[1].SensorType": "NO",
    "table.Alarm[3].Enable": "true",
    "table.Alarm[3].Name": "Alarm-in Port4",
    "table.Alarm[3].SensorType": "NO",
}


def alarm_local(index, action="Start"):
    body = "Code=AlarmLocal;action=%s;index=%d\r\n" % (action, index)
    return (
        b"--myboundary\n"
        b"Content-Type: text/plain\n"
        b"Content-Length: 40\n"
        b"\n" + body.encode()
    )


@pytest.fixture(autouse=True)
def _clean_streams():
    dahua_module._HOST_STREAMS.clear()
    yield
    dahua_module._HOST_STREAMS.clear()


@pytest.fixture(autouse=True)
async def _stop_host_streams(hass):
    yield
    pending = []
    for stream in list(dahua_module._HOST_STREAMS.values()):
        for attribute in ("_task", "_video_motion_poll_task"):
            task = getattr(stream, attribute, None)
            if task is not None and not task.done():
                task.cancel()
                pending.append(task)
    dahua_module._HOST_STREAMS.clear()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


class _Client:
    def __init__(self):
        self.attached_with = []

    async def stream_events(self, on_receive, events, channel):
        self.attached_with.append(list(events))
        await asyncio.Event().wait()


class _Coordinator:
    """A channel. `alarm_inputs` is what its probe found, usually nothing."""

    def __init__(self, channel, events=("VideoMotion",), alarm_inputs=()):
        self._channel = channel
        self.events = list(events)
        self.client = _Client()
        self.handled = []
        self._alarm_inputs = list(alarm_inputs)
        self.state_reads = 0

    def get_channel(self):
        return self._channel

    def get_address(self):
        return ADDRESS

    def handle_event(self, event):
        self.handled.append(event)

    def supports_alarm_inputs(self):
        return bool(self._alarm_inputs)

    def get_alarm_inputs(self):
        return list(self._alarm_inputs)

    async def _async_refresh_alarm_input_state(self):
        self.state_reads += 1


async def _settle():
    await asyncio.sleep(0)
    await asyncio.sleep(0)


# --- the parsers, against the bytes the device sent -------------------------


def test_the_alarm_table_parses_into_one_entry_per_terminal():
    assert parse_alarm_inputs(ALARM_TABLE) == [
        {"index": 0, "name": "Alarm-in Port1", "enabled": True},
        {"index": 1, "name": "Alarm-in Port2", "enabled": False},
        {"index": 3, "name": "Alarm-in Port4", "enabled": True},
    ]


def test_the_linkage_tree_is_not_carried_into_an_entry():
    """EventHandler describes what the recorder does, not what the terminal is.

    It is the bulk of the real payload -- forty-odd keys per input, including
    a nested TimeSection matrix -- and none of it belongs on a binary sensor.
    """
    for entry in parse_alarm_inputs(ALARM_TABLE):
        assert set(entry) == {"index", "name", "enabled"}


def test_a_device_without_the_table_yields_no_terminals():
    """`getConfig` for an absent table answers 200 with nothing, not an error.

    So the probe has to judge on what came back. A camera must end up with an
    empty list here, or it gets alarm sensors it has no terminals for.
    """
    assert parse_alarm_inputs({}) == []
    assert parse_alarm_inputs({"table.Lighting[0].Mode": "Auto"}) == []


def test_the_state_bitmask_is_read_at_the_measured_bit_positions():
    """result=8 is terminal 3 and result=16384 is terminal 14, both measured."""
    assert parse_alarm_state_bitmask({"result": "8"}, 16) == {3}
    assert parse_alarm_state_bitmask({"result": "16384"}, 16) == {14}
    assert parse_alarm_state_bitmask({"result": "0"}, 16) == set()
    assert parse_alarm_state_bitmask({"result": "9"}, 16) == {0, 3}


def test_the_bitmask_is_bounded_by_the_slot_count():
    """A bit above the terminals the device admits to is not a terminal."""
    assert parse_alarm_state_bitmask({"result": str(1 << 20)}, 16) == set()


def test_an_unreadable_state_answer_is_not_a_known_inactive_state():
    for answer in ({}, {"result": "Error"}, {"result": None}, {"result": "-1"}):
        with pytest.raises(ValueError):
            parse_alarm_state_bitmask(answer, 16)


# --- the index, which is the whole bug --------------------------------------


def test_the_event_index_is_the_zero_based_table_position():
    """Measured: Alarm[3], whose own label says Port4, sends index=3.

    If this ever reads 4, every sensor is named one terminal off.
    """
    assert alarm_input_index({"index": "3"}) == 3
    assert alarm_input_index({"index": "14"}) == 14


def test_both_casings_are_read():
    """DHIP sends `Index`, the CGI wire format parses to `index`."""
    assert alarm_input_index({"Index": 3}) == 3
    assert alarm_input_index({"index": 3}) == 3


def test_an_unreadable_index_is_the_first_terminal():
    for event in ({}, {"index": ""}, {"index": "x"}, {"index": "-2"}):
        assert alarm_input_index(event) == 0


def test_each_terminal_gets_its_own_event_code():
    assert alarm_input_event_code(3) == "AlarmInput_3"
    assert alarm_input_event_code(0) != alarm_input_event_code(1)


# --- the dispatch -----------------------------------------------------------


async def test_a_terminal_does_not_reach_the_camera_on_that_channel(hass):
    """The bug, named. Terminal 3 must not touch channel 3's camera."""
    stream = _host_stream(hass, ADDRESS)
    host = _Coordinator(0, alarm_inputs=[{"index": 3, "name": "Alarm-in Port4"}])
    camera3 = _Coordinator(3)
    for coordinator in (host, camera3):
        stream.register(coordinator)
    await _settle()

    stream.on_receive(alarm_local(3), 0)

    assert camera3.handled == [], (
        "terminal 3 was delivered to the camera on channel 3, which is the "
        "bug: on the measured recorder that was a rear lane camera with no "
        "alarm terminal"
    )
    assert [e["Code"] for e in host.handled] == ["AlarmLocal"]
    assert host.handled[0]["index"] == "3"


async def test_a_terminal_above_the_channel_count_still_arrives(hass):
    """Nothing bounds a terminal number by how many cameras are configured."""
    stream = _host_stream(hass, ADDRESS)
    host = _Coordinator(0, alarm_inputs=[{"index": 14, "name": "Alarm-in Port15"}])
    stream.register(host)
    stream.register(_Coordinator(1))
    await _settle()

    stream.on_receive(alarm_local(14), 0)

    assert len(host.handled) == 1


async def test_other_codes_still_go_to_their_own_channel(hass):
    """Only AlarmLocal is re-routed. Everything else keeps its channel."""
    stream = _host_stream(hass, ADDRESS)
    host = _Coordinator(0, alarm_inputs=[{"index": 0, "name": "Alarm-in Port1"}])
    camera2 = _Coordinator(2)
    for coordinator in (host, camera2):
        stream.register(coordinator)
    await _settle()

    stream.on_receive(
        b"--myboundary\nContent-Type: text/plain\n\n"
        b"Code=VideoMotion;action=Start;index=2\r\n",
        0,
    )

    assert [e["Code"] for e in camera2.handled] == ["VideoMotion"]
    assert host.handled == []


async def test_a_host_with_no_terminals_keeps_the_old_behaviour(hass):
    """A camera, and a recorder whose probe found nothing, are untouched.

    #231's single-channel branch depends on this: a doorbell sends AlarmLocal
    with a nonzero index and there is no channel to disambiguate.
    """
    stream = _host_stream(hass, ADDRESS)
    only = _Coordinator(0, events=["AlarmLocal"])
    stream.register(only)
    await _settle()

    stream.on_receive(alarm_local(1), 0)

    assert [e["Code"] for e in only.handled] == ["AlarmLocal"]


async def test_a_double_without_the_method_does_not_break_the_stream(hass):
    """Every coordinator in the suite is asked this. Most have never heard of it."""

    class _Old:
        events = ["VideoMotion"]

        def __init__(self):
            self.client = _Client()
            self.handled = []

        def get_channel(self):
            return 0

        def get_address(self):
            return ADDRESS

        def handle_event(self, event):
            self.handled.append(event)

    stream = _host_stream(hass, ADDRESS)
    stream.register(_Old())
    await _settle()
    assert stream._alarm_input_coordinator() is None


# --- the subscription -------------------------------------------------------


async def test_alarm_local_stays_subscribed_for_the_host_sensors(hass):
    """No channel speaks for a terminal, so no channel can unsubscribe one.

    Deselecting AlarmLocal on every camera would otherwise stop the stream
    carrying it, and the host sensors would hold whatever the setup state read
    found for ever.
    """
    stream = _host_stream(hass, ADDRESS)
    host = _Coordinator(
        0, events=["VideoMotion"], alarm_inputs=[{"index": 0, "name": "Port1"}]
    )
    stream.register(host)
    await _settle()

    assert "AlarmLocal" in stream._union()


async def test_a_host_without_terminals_does_not_gain_the_subscription(hass):
    stream = _host_stream(hass, ADDRESS)
    stream.register(_Coordinator(0, events=["VideoMotion"]))
    await _settle()

    assert "AlarmLocal" not in stream._union()


# --- the state read on re-attach --------------------------------------------


async def test_the_terminals_are_re_read_when_the_stream_comes_back(hass):
    """A Stop sent while the socket was down is gone, and the sensor is still on.

    These have a real state read, so there is nothing to infer.
    """
    stream = _host_stream(hass, ADDRESS)
    host = _Coordinator(0, alarm_inputs=[{"index": 0, "name": "Port1"}])
    stream.register(host)
    await _settle()

    await stream._async_reconcile_alarm_inputs()

    assert host.state_reads == 1


async def test_nothing_is_re_read_on_a_host_with_no_terminals(hass):
    """A failing stream retries often; this must not add load to a sick device."""
    stream = _host_stream(hass, ADDRESS)
    plain = _Coordinator(0)
    stream.register(plain)
    await _settle()

    await stream._async_reconcile_alarm_inputs()

    assert plain.state_reads == 0


async def test_a_failing_state_read_does_not_escape_the_stream_loop(hass):
    """It is called from the retry path; a raise there stops every reconnect."""

    class _Angry(_Coordinator):
        async def _async_refresh_alarm_input_state(self):
            raise RuntimeError("the device hung up")

    stream = _host_stream(hass, ADDRESS)
    stream.register(_Angry(0, alarm_inputs=[{"index": 0, "name": "Port1"}]))
    await _settle()

    await stream._async_reconcile_alarm_inputs()


async def test_a_raising_handler_does_not_take_the_stream_down(hass):
    """#706's rule: on_receive is wrapped in try/finally with no handler."""

    class _Angry(_Coordinator):
        def handle_event(self, event):
            raise RuntimeError("boom")

    stream = _host_stream(hass, ADDRESS)
    host = _Angry(0, alarm_inputs=[{"index": 3, "name": "Alarm-in Port4"}])
    stream.register(host)
    await _settle()

    stream.on_receive(alarm_local(3), 0)


def test_the_stream_helper_is_safe_on_a_bare_instance():
    """Several test files build a stream with object.__new__."""
    stream = object.__new__(DahuaHostEventStream)
    stream._by_channel = {}
    assert stream._alarm_input_coordinator() is None


# --- the probe, and the state it seeds --------------------------------------


class _ProbeClient:
    """Answers the three reads the probe makes, or refuses one."""

    def __init__(self, slots=16, table=None, state="0", raises=None):
        self._slots = slots
        self._table = ALARM_TABLE if table is None else table
        self._state = state
        self._raises = raises
        self.state_reads = 0

    async def async_get_alarm_input_slots(self):
        if self._raises == "slots":
            raise RuntimeError("no")
        return self._slots

    async def async_get_alarm_input_config(self):
        if self._raises == "table":
            raise RuntimeError("no")
        return self._table

    async def async_get_alarm_input_state(self, slots):
        self.state_reads += 1
        if self._raises == "state":
            raise RuntimeError("no")
        return parse_alarm_state_bitmask({"result": self._state}, slots)


def _coordinator(client):
    """A coordinator built the way this suite builds them, with only what the
    probe touches set. The class defaults do the rest, which is the point of
    their being on the class."""
    coordinator = object.__new__(DahuaDataUpdateCoordinator)
    coordinator.client = client
    coordinator._address = ADDRESS
    coordinator._channel = 0
    coordinator._dahua_event_timestamp = {}
    return coordinator


async def test_the_probe_finds_the_terminals():
    coordinator = _coordinator(_ProbeClient())
    await coordinator._async_probe_alarm_inputs()

    assert coordinator.supports_alarm_inputs() is True
    assert [i["index"] for i in coordinator.get_alarm_inputs()] == [0, 1, 3]


async def test_a_device_without_the_table_gets_no_terminals():
    """Judged on what came back. `async_get_config` swallows a
    ClientResponseError and returns {}, and a device answers 200 with an empty
    body for a table it does not have -- so "it did not raise" proves nothing.
    """
    coordinator = _coordinator(_ProbeClient(slots=0, table={}))
    await coordinator._async_probe_alarm_inputs()

    assert coordinator.supports_alarm_inputs() is False
    assert coordinator.get_alarm_inputs() == []


@pytest.mark.parametrize("refused", ["slots", "table"])
async def test_a_refused_probe_does_not_take_setup_down(refused):
    """It runs inside the one-time init block, which has no return_exceptions.

    An unguarded raise here is setup_retry for the whole recorder, over a
    feature most of them do not have.
    """
    coordinator = _coordinator(_ProbeClient(raises=refused))
    await coordinator._async_probe_alarm_inputs()

    assert coordinator.supports_alarm_inputs() is False


async def test_the_state_read_seeds_a_terminal_that_is_already_closed():
    """Without this, a restart while a door is held open reads off until the
    next edge, and the next edge may be hours away."""
    coordinator = _coordinator(_ProbeClient(state="8"))
    await coordinator._async_probe_alarm_inputs()

    assert coordinator._dahua_event_timestamp["AlarmInput_3-0"] > 0
    assert coordinator._dahua_event_timestamp["AlarmInput_0-0"] == 0


async def test_a_terminal_that_opened_while_the_stream_was_down_is_cleared():
    """The Stop was sent and nobody was listening, so the sensor is still on."""
    coordinator = _coordinator(_ProbeClient(state="8"))
    await coordinator._async_probe_alarm_inputs()
    assert coordinator._dahua_event_timestamp["AlarmInput_3-0"] > 0

    coordinator.client._state = "0"
    await coordinator._async_refresh_alarm_input_state()

    assert coordinator._dahua_event_timestamp["AlarmInput_3-0"] == 0


async def test_a_terminal_still_closed_keeps_its_original_start_time():
    """A reattach is not a new alarm. Restating it would make every reconnect
    look like the door was opened again.

    The start time is backdated by an hour first, deliberately: comparing a
    fresh probe against an immediate re-read passes whether or not the time is
    preserved, because both land in the same second. That version of this test
    did not fail when the preservation was removed.
    """
    coordinator = _coordinator(_ProbeClient(state="8"))
    await coordinator._async_probe_alarm_inputs()
    opened_an_hour_ago = int(time.time()) - 3600
    coordinator._dahua_event_timestamp["AlarmInput_3-0"] = opened_an_hour_ago

    await coordinator._async_refresh_alarm_input_state()

    assert coordinator._dahua_event_timestamp["AlarmInput_3-0"] == opened_an_hour_ago


async def test_a_failed_state_read_leaves_the_sensors_where_they_were():
    """Blanking them on a failed read is worse than a stale state: it reports
    every door shut because one request timed out."""
    coordinator = _coordinator(_ProbeClient(state="8"))
    await coordinator._async_probe_alarm_inputs()
    first = coordinator._dahua_event_timestamp["AlarmInput_3-0"]

    coordinator.client._raises = "state"
    await coordinator._async_refresh_alarm_input_state()

    assert coordinator._dahua_event_timestamp["AlarmInput_3-0"] == first


@pytest.mark.parametrize("answer", ["Error", None, "-1"])
async def test_an_unreadable_success_response_does_not_clear_active_inputs(answer):
    coordinator = _coordinator(_ProbeClient(state="8"))
    await coordinator._async_probe_alarm_inputs()
    first = coordinator._dahua_event_timestamp["AlarmInput_3-0"]
    coordinator.client._state = answer
    await coordinator._async_refresh_alarm_input_state()
    assert coordinator._dahua_event_timestamp["AlarmInput_3-0"] == first


async def test_nothing_is_read_for_a_device_with_no_terminals():
    coordinator = _coordinator(_ProbeClient(table={}))
    await coordinator._async_probe_alarm_inputs()
    before = coordinator.client.state_reads

    await coordinator._async_refresh_alarm_input_state()

    assert coordinator.client.state_reads == before


async def test_the_table_decides_when_the_slot_count_disagrees():
    """The entities are built from the table, so it wins. getInSlots only
    bounds the bitmask scan."""
    coordinator = _coordinator(_ProbeClient(slots=0, state="8"))
    await coordinator._async_probe_alarm_inputs()

    assert len(coordinator.get_alarm_inputs()) == 3
    assert coordinator._alarm_input_slots == 4
    assert coordinator._dahua_event_timestamp["AlarmInput_3-0"] > 0
