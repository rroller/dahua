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

import aiohttp

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
        self.table_reads = 0
        self.attached_with = []

    async def async_get_alarm_input_slots(self):
        if self._raises == "slots":
            raise RuntimeError("no")
        return self._slots

    async def async_get_alarm_input_config(self):
        self.table_reads += 1
        # ⚠ Suspends, because a coroutine that does not is not a request.
        # Awaiting one that never yields runs to completion before the next
        # task starts, so two channels retrying through `asyncio.gather` go one
        # after the other and the check that decides between them is never
        # reached. Removing the check inside the probe passed every test here
        # until this line existed.
        await asyncio.sleep(0)
        if self._raises == "table":
            raise RuntimeError("no")
        return self._table

    async def async_get_alarm_input_state(self, slots):
        self.state_reads += 1
        if self._raises == "state":
            raise RuntimeError("no")
        return parse_alarm_state_bitmask({"result": self._state}, slots)

    async def stream_events(self, on_receive, events, channel):
        """Held open like `_Client`'s, for the tests that register a real
        coordinator on a real host stream and then read what it attached
        with."""
        self.attached_with.append(set(events))
        await asyncio.Event().wait()


def _coordinator(client, device_class="NVR"):
    """A coordinator built the way this suite builds them, with only what the
    probe touches set. The class defaults do the rest, which is the point of
    their being on the class.

    The class the device reported, because the probe gates on it: terminals are
    a recorder's, and the gate lives inside the probe so that the retry applies
    the same one. Set rather than stubbed over `is_recorder_host`, so a test
    goes through the same reading of the same answer production does.
    """
    coordinator = object.__new__(DahuaDataUpdateCoordinator)
    coordinator.client = client
    coordinator._address = ADDRESS
    coordinator._channel = 0
    coordinator._dahua_event_timestamp = {}
    coordinator._device_class = device_class
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


# --- asked again, when the box said nothing ---------------------------------
#
# The last of the three probes I listed on #1213 as outside the retry. A box
# that did not answer this one kept no record of it, so the question was
# neither re-asked nor visible in the dump, and the terminals were gone for the
# life of the entry.
#
# The host scope is what makes it different from the other two. Every channel
# asks, the lowest that found terminals carries the entities, and `host.py`
# routes `AlarmLocal` to the same one. A retry that let a lower channel adopt
# them afterwards would move the carrier away from the entities, which exist
# under a unique id taken from the box's serial and cannot follow.


def _refused(status=403):
    """The refusal a device's 403 actually arrives as.

    ⚠ `aiohttp.ClientResponseError` and not a plain exception carrying a
    `status`. `async_get_config` swallows exactly this type and returns `{}`,
    so a hand-rolled stand-in passes through the lenient reader untouched and
    a test using one cannot tell the two readers apart. Reverting the strict
    read survived this harness until this was the real type.
    """
    return aiohttp.ClientResponseError(None, (), status=status, message="Forbidden")


def _real(client, device_class="NVR", channel=0, hass=None, events=("VideoMotion",)):
    """A real coordinator, since what is under test is its own methods."""
    coordinator = _coordinator(client, device_class=device_class)
    coordinator._channel = channel
    coordinator.hass = hass
    coordinator.events = list(events)
    return coordinator


async def test_a_silent_box_leaves_the_question_open():
    """The bug. The probe caught everything into a debug line, so the capability
    was neither retried nor in the dump, and five screw terminals went missing
    for good."""
    coordinator = _real(_ProbeClient(raises="table"))

    assert await coordinator._async_probe_alarm_inputs() is False
    assert coordinator.probes_that_did_not_answer() == {"alarm_inputs"}


@pytest.mark.parametrize("read", ["async_get_alarm_input_slots", "config"])
async def test_a_refusal_is_the_box_answering(read):
    """A status is an answer, and it is the half worth keeping: it says this
    model does not serve the table, which generalises, where a timeout says
    only that this moment was bad.

    Both reads, and this is the probe's half only: the client is a double, so
    what it proves is that a refusal reaching the probe is recorded with its
    status. Whether a refusal reaches it at all is the client's half, and the
    test below is that one. The table read used to go through
    `async_get_config`, which turns a 403 into `{}`, so production would have
    filed the refusal as "this box has no terminals" while this test passed on
    a double that raises.
    """
    client = _ProbeClient()

    async def refuse(*_args):
        raise _refused()

    if read == "config":
        client.async_get_alarm_input_config = refuse
    else:
        client.async_get_alarm_input_slots = refuse
    coordinator = _real(client)

    assert await coordinator._async_probe_alarm_inputs() is False
    assert coordinator._probe_refusals["alarm_inputs"]["status"] == 403
    assert coordinator.probes_that_did_not_answer() == set()


async def test_the_table_read_names_the_table_and_does_not_swallow():
    """Both halves of the read the record depends on.

    The URL, because a name the box does not have answers 200 with an empty
    body and reads back as "no terminals" for ever (#1194). And the refusal,
    because `async_get_config` would turn it into the same empty answer. Same
    URL as `async_get_config("Alarm")` would build, so the two share one entry
    in the read cache.
    """
    from custom_components.dahua.client import DahuaClient

    client = object.__new__(DahuaClient)
    asked = []

    async def get(url):
        asked.append(url)
        raise _refused()

    client.get = get

    with pytest.raises(aiohttp.ClientResponseError):
        await client.async_get_alarm_input_config()

    assert asked == ["/cgi-bin/configManager.cgi?action=getConfig&name=Alarm"]


async def test_a_box_with_no_terminals_has_answered():
    """Answering "no terminals" is answering. Nothing should keep asking, and
    the leftover-rows card should not wait on it."""
    coordinator = _real(_ProbeClient(table={}))

    assert await coordinator._async_probe_alarm_inputs() is False
    assert coordinator.probes_that_did_not_answer() == set()


async def test_a_class_that_has_not_answered_is_not_chased():
    """The limit, stated rather than worked around.

    The gate is `is_recorder_host()`, which reads the class the device
    reported, so a box that has not answered that question is not asked this
    one. Nothing is recorded, which means nothing comes back to it either: a
    recorder whose class answers on a later poll gets its terminals after a
    reload, the same as its disks, its remote devices and its host device,
    which are all decided from that flag in the same block.

    Recording it as open instead reaches further than it looks. The flag is
    true for any device whose class probe timed out, cameras included, and the
    platform reads a pending answer as terminals still to come: a camera would
    lose the per-channel AlarmLocal sensor it has today and be re-asked for a
    table it does not have until the budget ran out, with a give-up line to
    show for it.
    """
    coordinator = _real(_ProbeClient(), device_class=None)
    coordinator._device_class_unanswered = True

    assert await coordinator._async_probe_alarm_inputs() is False
    assert coordinator.probes_that_did_not_answer() == set()
    assert "alarm_inputs" not in (getattr(coordinator, "_probe_refusals", None) or {})
    assert coordinator.client.table_reads == 0


async def test_a_camera_is_not_asked_and_not_recorded():
    """A camera answered the class question, and the answer was not a recorder.
    That is a settled no rather than a question outstanding, so recording it
    would have every camera re-asking for a table it does not have."""
    coordinator = _real(_ProbeClient(), device_class="IPC")

    assert await coordinator._async_probe_alarm_inputs() is False
    assert coordinator.probes_that_did_not_answer() == set()
    assert "alarm_inputs" not in (getattr(coordinator, "_probe_refusals", None) or {})


def test_the_retry_registry_carries_the_terminals():
    """Through the registry, because that is the only thing
    `_async_retry_unanswered_capabilities` reads. A retry method nothing
    registers is never called."""
    from custom_components.dahua.coordinator import RETRYABLE_CAPABILITIES

    assert (
        RETRYABLE_CAPABILITIES["alarm_inputs"]
        is DahuaDataUpdateCoordinator._async_retry_alarm_inputs
    )


async def test_the_retry_adopts_the_terminals_and_widens_the_subscription(hass):
    """The whole point, end to end on the host.

    The subscription is host level as well: `_union` adds `AlarmLocal` only
    where a carrier exists, so a box where no channel selected that code was
    not subscribed to the one thing these sensors are built from. Without the
    nudge the entities appear and never move.
    """
    stream = _host_stream(hass, ADDRESS)
    coordinator = _real(_ProbeClient(raises="table"), hass=hass)
    stream.register(coordinator)
    await _settle()

    assert await coordinator._async_probe_alarm_inputs() is False
    assert coordinator.client.attached_with == [{"VideoMotion"}]

    coordinator.client._raises = None

    assert await coordinator._async_retry_alarm_inputs() is True
    assert [i["index"] for i in coordinator.get_alarm_inputs()] == [0, 1, 3]
    assert coordinator.probes_that_did_not_answer() == set()
    await _settle()

    assert "AlarmLocal" in stream._events, (
        "the stream is still attached without AlarmLocal, so the new sensors "
        "would sit at whatever the setup state read found"
    )
    assert (
        len(coordinator.client.attached_with) == 2
    ), "the wanted set changed but the stream was never re-attached: %s" % (
        coordinator.client.attached_with,
    )
    # On the wire that second attach is `[All]`, not the two codes. Widening
    # the set past what any one channel asked for is what #615's heuristic
    # reads as sharing, and some firmware goes silent on the expanded explicit
    # list. Asserted on the set the stream decided it wants, which is the thing
    # this nudge changes either way.
    assert coordinator.client.attached_with[-1] == {"All"}


async def test_the_retry_reports_nothing_it_already_had(hass):
    """A retry that announces a discovery it already holds rebuilds every
    platform on every poll."""
    stream = _host_stream(hass, ADDRESS)
    coordinator = _real(_ProbeClient(), hass=hass)
    stream.register(coordinator)
    await _settle()
    assert await coordinator._async_probe_alarm_inputs() is True

    assert await coordinator._async_retry_alarm_inputs() is False


async def test_the_retry_leaves_the_carrier_where_it_is(hass):
    """The case the host scope creates, and the reason this retry is not the
    same shape as the other two.

    Channel 3 found the terminals at setup, so the entities were created on it,
    under a unique id taken from the box's serial. Let channel 0 adopt them on
    a retry and `host.py` routes the next `AlarmLocal` to channel 0, while the
    entities stay on channel 3 and never change again.
    """
    stream = _host_stream(hass, ADDRESS)
    carrier = _Coordinator(3, alarm_inputs=[{"index": 0, "name": "Port1"}])
    coordinator = _real(_ProbeClient(raises="table"), hass=hass)
    for registered in (coordinator, carrier):
        stream.register(registered)
    await _settle()
    assert await coordinator._async_probe_alarm_inputs() is False
    coordinator.client._raises = None
    reads_before = coordinator.client.table_reads

    assert await coordinator._async_retry_alarm_inputs() is False
    assert coordinator.get_alarm_inputs() == [], (
        "channel 0 adopted terminals channel 3 is already carrying, so the "
        "events now go to a coordinator with no sensors on it"
    )
    assert dahua_module.async_alarm_input_carrier(ADDRESS) is carrier
    assert coordinator.probes_that_did_not_answer() == set()
    # And it did not read to find that out. Twelve channels each re-asking a
    # question the box has already answered is twelve requests a recorder logs
    # a login for, three times over, for nothing. The check inside the probe
    # is the one that decides; this one is the one that keeps it cheap.
    assert coordinator.client.table_reads == reads_before


async def test_two_retries_at_once_leave_one_carrier(hass):
    """Both channels check before reading, so both pass that check. The check
    that decides is the one inside the probe, at the adoption, with no await
    between it and the assignment."""
    stream = _host_stream(hass, ADDRESS)
    channels = [
        _real(_ProbeClient(raises="table"), channel=index, hass=hass)
        for index in (0, 1)
    ]
    for coordinator in channels:
        stream.register(coordinator)
    await _settle()
    for coordinator in channels:
        assert await coordinator._async_probe_alarm_inputs() is False
        coordinator.client._raises = None

    await asyncio.gather(*(c._async_retry_alarm_inputs() for c in channels))

    holding = [c for c in channels if c.supports_alarm_inputs()]
    assert len(holding) == 1, (
        "both channels adopted the box's terminals, so which one carries them "
        "depends on who is asked and the entities cannot be on both"
    )
    assert dahua_module.async_alarm_input_carrier(ADDRESS) is holding[0]


async def test_the_nudge_does_not_invent_a_stream(hass):
    """A channel with no events selected never registers one, so the retry can
    reach for a host that has no stream. Creating it here would leave one with
    no coordinator to attach for, holding a connector and a retry loop."""
    dahua_module.async_refresh_host_subscription("10.9.9.9")

    assert dahua_module._HOST_STREAMS == {}


async def test_the_carrier_is_read_through_the_host(hass):
    """One source for who carries them, because `host.py` routes with it and
    binary_sensor.py builds with it. Two copies of the rule is two rules."""
    assert dahua_module.async_alarm_input_carrier(ADDRESS) is None
    stream = _host_stream(hass, ADDRESS)
    carrier = _Coordinator(3, alarm_inputs=[{"index": 0, "name": "Port1"}])
    stream.register(_Coordinator(0))
    stream.register(carrier)
    await _settle()

    assert dahua_module.async_alarm_input_carrier(ADDRESS) is carrier
    assert dahua_module.async_alarm_input_carrier("10.9.9.9") is None
