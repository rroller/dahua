"""The flow wiring behind #186, as opposed to the decision behind it.

test_discover_channels.py pins *which* channels are worth offering. Nothing
pinned what the flow then does with them, which left the three new steps
untested: the parsers were proven and the wiring around them was not.

What matters here:

- discovery never fails a setup. A standalone camera has no RemoteDevice table,
  and a recorder that stops answering must not leave somebody unable to add the
  one camera they asked for
- the channel being added is not offered back to itself
- a slot that survives the table check and then fails the probe is dropped. That
  is the removed-camera case the whole feature turns on
- each extra channel becomes its own flow, carrying the same credentials and its
  own channel number
"""
import asyncio
from types import SimpleNamespace

import pytest

from homeassistant.data_entry_flow import FlowResultType

from custom_components.dahua.config_flow import DahuaFlowHandler
from custom_components.dahua.const import (
    CONF_ADDRESS,
    CONF_CHANNEL,
    CONF_ALL_CHANNELS,
    CONF_EXTRA_CHANNELS,
    CONF_NAME,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_RTSP_PORT,
    CONF_USERNAME,
)

async def _noop(*args, **kwargs):
    return None


INFO = "table.RemoteDevice.uuid:System_CONFIG_NETCAMERA_INFO_{0}.{1}"


def _entry_data(channel=0):
    return {
        CONF_USERNAME: "admin",
        CONF_PASSWORD: "pw",
        CONF_ADDRESS: "10.0.0.5",
        CONF_PORT: "80",
        CONF_RTSP_PORT: "554",
        CONF_CHANNEL: channel,
        CONF_NAME: "Front",
    }


def _flow():
    flow = DahuaFlowHandler()
    flow.init_info = _entry_data()
    return flow


class _Client:
    """A recorder that answers the three reads discovery makes."""

    def __init__(self, slots, live, titles=None, remote_raises=None):
        self.slots = slots
        self.live = set(live)
        self.titles = titles or {}
        self.remote_raises = remote_raises
        self.probed = []

    async def async_get_remote_devices(self):
        if self.remote_raises:
            raise self.remote_raises
        data = {}
        for index, (enabled, protocol) in self.slots.items():
            data[INFO.format(index, "Enable")] = "true" if enabled else "false"
            data[INFO.format(index, "ProtocolType")] = protocol
        return data

    async def async_get_config(self, name):
        return {
            "table.ChannelTitle[{0}].Name".format(i): t
            for i, t in self.titles.items()
        }

    async def async_probe_snapshot(self, channel_number):
        self.probed.append(channel_number)
        if (channel_number - 1) not in self.live:
            raise RuntimeError("no camera there")


async def _discover(monkeypatch, client, exclude=0):
    """Run discovery against `client`, with the network parts stubbed out."""
    import custom_components.dahua.config_flow as flow_module

    class _Session:
        async def close(self):
            return None

    monkeypatch.setattr(flow_module, "ClientSession", lambda **kw: _Session())
    monkeypatch.setattr(flow_module, "TCPConnector", lambda **kw: None)
    monkeypatch.setattr(flow_module, "DahuaClient", lambda *a, **kw: client)

    return await _flow()._async_discover_channels(_entry_data(), exclude)


# --- discovery never breaks an ordinary setup -------------------------------

async def test_a_camera_with_no_remote_device_table_offers_nothing(monkeypatch):
    """A standalone camera. Ordinary, not a failure."""
    client = _Client({}, [], remote_raises=RuntimeError("no such table"))

    assert await _discover(monkeypatch, client) == {}


async def test_a_recorder_that_stops_answering_offers_nothing(monkeypatch):
    class _Dead:
        async def async_get_remote_devices(self):
            raise TimeoutError()

    assert await _discover(monkeypatch, _Dead()) == {}


# --- what gets offered ------------------------------------------------------

async def test_only_channels_that_answer_are_offered(monkeypatch):
    """The removed-camera case, and the reason this probes at all.

    Slot 2 is enabled with a plausible entry and no camera behind it.
    """
    client = _Client(
        slots={0: (True, "Private"), 1: (True, "Private"), 2: (True, "Private")},
        live=[0, 1],
    )

    found = await _discover(monkeypatch, client, exclude=0)

    assert sorted(found) == [1]


async def test_the_channel_being_added_is_not_offered_back(monkeypatch):
    client = _Client(
        slots={0: (True, "Private"), 1: (True, "Private")}, live=[0, 1])

    found = await _discover(monkeypatch, client, exclude=0)

    assert 0 not in found


async def test_a_switched_off_slot_is_not_even_probed(monkeypatch):
    client = _Client(
        slots={0: (True, "Private"), 5: (False, "Private")}, live=[0, 5])

    await _discover(monkeypatch, client, exclude=0)

    assert 6 not in client.probed, "an empty slot cost a request"


async def test_an_onvif_slot_is_not_even_probed(monkeypatch):
    client = _Client(
        slots={0: (True, "Private"), 3: (True, "Onvif")}, live=[0, 3])

    await _discover(monkeypatch, client, exclude=0)

    assert 4 not in client.probed


async def test_the_recorders_own_name_is_used_as_the_label(monkeypatch):
    client = _Client(
        slots={0: (True, "Private"), 1: (True, "Private")}, live=[0, 1],
        titles={1: "BACKYARD"})

    found = await _discover(monkeypatch, client, exclude=0)

    assert found[1] == "BACKYARD"


async def test_a_channel_with_no_title_still_gets_a_label(monkeypatch):
    client = _Client(
        slots={0: (True, "Private"), 1: (True, "Private")}, live=[0, 1])

    found = await _discover(monkeypatch, client, exclude=0)

    assert found[1] == "Channel 2", "the label is the channel number, not the index"


# --- the step, and what it queues -------------------------------------------

async def test_choosing_channels_records_them_and_moves_on():
    """It goes on to the areas step now, not straight to naming.

    Choosing channels is what makes the areas step worth showing, so the two are
    wired together -- see test_channel_areas.py. Naming still follows, one step
    later.
    """
    flow = _flow()
    flow._found_channels = {1: "BACKYARD", 2: "DRIVEWAY"}
    reached = {}

    async def areas():
        reached["yes"] = True

    flow.async_step_areas = areas

    await flow.async_step_channels({CONF_EXTRA_CHANNELS: ["1", "2"]})

    assert flow._extra_channels == [1, 2]
    assert reached.get("yes"), "it must go on to placing them"


async def test_choosing_channels_still_reaches_naming_through_the_areas_step():
    """The original contract, one step further along: whatever else happens,
    the user still gets to name the first device."""
    flow = _flow()
    flow._found_channels = {1: "BACKYARD"}
    shown = {}

    async def show(data):
        shown["data"] = data

    await flow.async_step_channels({CONF_EXTRA_CHANNELS: ["1"]})
    flow._show_config_form_name = show
    await flow.async_step_areas({})

    assert shown["data"] is flow.init_info


async def test_choosing_none_queues_nothing():
    flow = _flow()
    flow._found_channels = {1: "BACKYARD"}
    async def show(data):
        return None

    flow._show_config_form_name = show

    await flow.async_step_channels({CONF_EXTRA_CHANNELS: []})

    assert flow._extra_channels == []


# --- taking the lot in one click ---------------------------------------------

async def _chose(flow, user_input):
    """Run the step with `user_input` and return what it recorded."""
    async def show(data):
        return None

    flow._show_config_form_name = show
    await flow.async_step_channels(user_input)
    return flow._extra_channels


async def test_all_channels_takes_every_one_that_was_found():
    """A sixteen channel recorder is sixteen boxes, and somebody adding a
    recorder usually wants all of it."""
    flow = _flow()
    flow._found_channels = {1: "BACKYARD", 2: "DRIVEWAY", 5: "SHED"}

    assert await _chose(flow, {CONF_ALL_CHANNELS: True}) == [1, 2, 5]


async def test_all_channels_is_in_channel_order():
    """These become entries in this order, so it is what the user sees."""
    flow = _flow()
    flow._found_channels = {9: "NINE", 2: "TWO", 11: "ELEVEN"}

    assert await _chose(flow, {CONF_ALL_CHANNELS: True}) == [2, 9, 11]


async def test_all_channels_wins_over_the_ticked_list():
    """Ticking the switch must not also require clearing the boxes."""
    flow = _flow()
    flow._found_channels = {1: "ONE", 2: "TWO", 3: "THREE"}

    chosen = await _chose(
        flow, {CONF_ALL_CHANNELS: True, CONF_EXTRA_CHANNELS: ["2"]})

    assert chosen == [1, 2, 3]


async def test_leaving_it_off_still_honours_the_ticked_list():
    """No regression: the existing behaviour is the default."""
    flow = _flow()
    flow._found_channels = {1: "ONE", 2: "TWO", 3: "THREE"}

    chosen = await _chose(
        flow, {CONF_ALL_CHANNELS: False, CONF_EXTRA_CHANNELS: ["3"]})

    assert chosen == [3]


async def test_all_channels_with_nothing_found_chooses_nothing():
    """Cannot happen through the flow, which skips this step when the search
    found nothing, but it must not raise if it ever does."""
    flow = _flow()
    flow._found_channels = {}

    assert await _chose(flow, {CONF_ALL_CHANNELS: True}) == []


async def test_the_form_says_how_many_were_found():
    """The count is in the label and the description, so it has to be given."""
    flow = _flow()
    flow._found_channels = {1: "ONE", 4: "FOUR"}
    flow._errors = {}

    result = await flow.async_step_channels()

    assert result["description_placeholders"] == {"count": "2"}


async def test_the_form_offers_the_switch_and_the_list():
    flow = _flow()
    flow._found_channels = {1: "ONE"}
    flow._errors = {}

    result = await flow.async_step_channels()

    keys = [str(key) for key in result["data_schema"].schema]
    assert keys == [CONF_ALL_CHANNELS, CONF_EXTRA_CHANNELS], (
        "the switch is offered first, above the boxes it replaces")


def test_each_extra_channel_becomes_its_own_flow():
    flow = _flow()
    flow._found_channels = {1: "BACKYARD", 2: "DRIVEWAY"}
    flow._extra_channels = [1, 2]
    started = []
    flow.hass = SimpleNamespace(
        async_create_task=lambda coro: None,
        config_entries=SimpleNamespace(flow=SimpleNamespace(
            async_init=lambda domain, context=None, data=None: started.append(data))),
    )

    flow._queue_extra_channels()

    assert [d[CONF_CHANNEL] for d in started] == [1, 2]
    assert [d[CONF_NAME] for d in started] == ["BACKYARD", "DRIVEWAY"]
    assert all(d[CONF_PASSWORD] == "pw" for d in started), \
        "each flow needs the credentials the user already gave"


def test_queueing_does_not_mutate_the_first_entrys_data():
    """dict(self.init_info) rather than the same object.

    Sharing it would give the camera the user is naming the channel number of
    the last extra one.
    """
    flow = _flow()
    flow._found_channels = {1: "BACKYARD"}
    flow._extra_channels = [1]
    flow.hass = SimpleNamespace(
        async_create_task=lambda coro: None,
        config_entries=SimpleNamespace(flow=SimpleNamespace(
            async_init=lambda domain, context=None, data=None: None)),
    )

    flow._queue_extra_channels()

    assert flow.init_info[CONF_CHANNEL] == 0
    assert flow.init_info[CONF_NAME] == "Front"


def test_nothing_chosen_starts_no_flows():
    flow = _flow()
    flow._extra_channels = []
    started = []
    flow.hass = SimpleNamespace(
        async_create_task=lambda coro: started.append(coro),
        config_entries=SimpleNamespace(flow=SimpleNamespace(
            async_init=lambda **kw: None)),
    )

    flow._queue_extra_channels()

    assert started == []


# --- the step that actually creates the extra entries ------------------------

async def test_an_imported_channel_becomes_an_entry():
    flow = DahuaFlowHandler()
    created = {}

    async def credentials(*args):
        return {"name": "BACKYARD", "serialNumber": "SER1"}, None

    async def set_unique_id(unique_id):
        created["unique_id"] = unique_id

    flow._test_credentials = credentials
    flow.async_set_unique_id = set_unique_id
    flow._abort_if_unique_id_configured = lambda: None
    flow.async_create_entry = lambda title, data: created.update(
        {"title": title, "data": data}) or created

    await flow.async_step_import(_entry_data(channel=3))

    assert created["title"] == "Front"
    assert created["data"][CONF_CHANNEL] == 3
    assert created["unique_id"] == "SER1_3", \
        "a channel above zero has to be distinguishable from the device itself"


async def test_an_imported_channel_that_no_longer_answers_aborts():
    """Probed a moment ago, so this is rare, and it must not create anything."""
    flow = DahuaFlowHandler()
    aborted = {}

    async def credentials(*args):
        return None, "cannot_connect"

    flow._test_credentials = credentials
    flow.async_abort = lambda reason: aborted.setdefault("reason", reason)
    flow.async_create_entry = lambda **kw: pytest.fail("created an entry anyway")

    await flow.async_step_import(_entry_data(channel=3))

    assert aborted["reason"] == "cannot_connect"


# --- the routing, and the wait the user is shown --------------------------

def _routable(found):
    """A flow sitting at the point where the search has already finished."""
    flow = _flow()
    flow._discovery_task = _finished(found)
    return flow


def _finished(value):
    task = asyncio.Future()
    task.set_result(value)
    return task


async def test_the_search_is_shown_as_a_wait():
    """Up to DISCOVERY_TIMEOUT_SECONDS of nothing on screen reads as a hung
    setup. The dialog says what is happening, and quotes the same ceiling the
    code enforces rather than a number written out by hand."""
    import custom_components.dahua.config_flow as flow_module

    flow = DahuaFlowHandler()
    flow.hass = SimpleNamespace(
        async_create_task=lambda coro: asyncio.ensure_future(coro))

    async def credentials(*args):
        return {"name": "Front", "serialNumber": "SER1"}, None

    async def discover(user_input, exclude):
        return {}

    flow._test_credentials = credentials
    flow._async_discover_channels = discover
    flow.async_set_unique_id = _noop
    flow._abort_if_unique_id_configured = lambda: None

    result = await flow.async_step_user(_entry_data())

    assert result["type"] == FlowResultType.SHOW_PROGRESS
    assert result["step_id"] == "discover"
    assert result["description_placeholders"]["seconds"] == str(
        flow_module.DISCOVERY_TIMEOUT_SECONDS)
    await flow._discovery_task


async def test_being_asked_again_mid_search_keeps_waiting():
    """Found by running the flow against a real recorder.

    A progress step is re-entered for reasons other than its task finishing:
    asking Home Assistant for the flow's current state re-enters it, and so
    does reopening the dialog. Branching on whether the task exists rather
    than whether it is done meant the first of those read the result of a task
    still running, raised InvalidStateError into the handler that treats any
    failure as nothing found, and offered the user no channels at all.

    On the recorder this was measured against it lost all eleven, silently.
    """
    flow = _flow()
    flow._discovery_task = asyncio.Future()  # started, still running

    result = await flow.async_step_discover()

    assert result["type"] == FlowResultType.SHOW_PROGRESS
    assert result["step_id"] == "discover"
    assert flow._found_channels == {}, "nothing may be concluded yet"

    flow._discovery_task.cancel()


async def test_finding_channels_leads_to_the_channels_step():
    """Discovery and the step are tested apart; this is the join between them."""
    flow = _routable({1: "BACKYARD"})

    result = await flow.async_step_discover()

    assert result["type"] == FlowResultType.SHOW_PROGRESS_DONE
    assert result["step_id"] == "channels"
    assert flow._found_channels == {1: "BACKYARD"}


async def test_finding_none_goes_straight_to_naming():
    """A standalone camera must not gain a step that offers it nothing."""
    flow = _routable({})

    result = await flow.async_step_discover()

    assert result["step_id"] == "name"


async def test_abandoning_the_search_does_not_break_the_flow():
    """A cancelled progress task raises out of .result(), and CancelledError is
    not an Exception. Adding the one camera asked for must still work."""
    flow = _flow()
    flow._discovery_task = asyncio.Future()
    flow._discovery_task.cancel()

    result = await flow.async_step_discover()

    assert result["step_id"] == "name"
    assert flow._found_channels == {}


async def test_the_name_form_opens_after_a_search_that_found_nothing():
    """async_step_name was only ever reached with input before this. Progress
    hands it None, and _show_config_form_name reads user_input[CONF_NAME]."""
    flow = _flow()
    shown = {}
    flow.async_show_form = lambda **kw: shown.update(kw) or shown

    await flow.async_step_name()

    assert shown["step_id"] == "name"


async def test_a_recorder_that_probes_forever_gives_up(monkeypatch):
    """The ceiling. Sixteen channels at one request timeout each, two at a
    time, is minutes of a form that looks frozen during setup."""
    import custom_components.dahua.config_flow as flow_module

    class _Slow(_Client):
        async def async_probe_snapshot(self, channel_number):
            await asyncio.sleep(30)

    monkeypatch.setattr(flow_module, "DISCOVERY_TIMEOUT_SECONDS", 0.1)
    client = _Slow(
        slots={0: (True, "Private"), 1: (True, "Private")}, live=[0, 1])

    found = await _discover(monkeypatch, client, exclude=0)

    assert found == {}, "a hung recorder must not hold the form open"


async def test_creating_the_first_entry_starts_the_others():
    """The join at the other end.

    _queue_extra_channels is tested directly above and nothing tested that
    anything calls it. Dropping the call leaves every other test here green
    and the feature doing nothing at all.
    """
    flow = _flow()
    flow._found_channels = {1: "BACKYARD"}
    flow._extra_channels = [1]
    started = []
    flow.hass = SimpleNamespace(
        async_create_task=lambda coro: None,
        config_entries=SimpleNamespace(flow=SimpleNamespace(
            async_init=lambda domain, context=None, data=None: started.append(data))),
    )
    flow.async_create_entry = lambda title, data: None

    await flow.async_step_name({"name": "Front Door"})

    assert [d[CONF_CHANNEL] for d in started] == [1]
