"""The switch for a PTZ camera's AI tour plan (#1051).

@MassakerGU was starting and stopping `IntelliTour[1].Enable` from outside
Home Assistant on a DH-SDT6E432-8P-MB-A-PV1, firmware V3.200.0000028.0.R, and
asked for a switch. Both transports were measured working on that camera:

    /cgi-bin/configManager.cgi?action=setConfig&IntelliTour[1].Enable=true
    {"method": "configManager.setConfig",
     "params": {"name": "IntelliTour[1].Enable", "table": true}}

The index is what this file is mostly about. The read they posted is:

    table.IntelliTour[0]          null
    table.IntelliTour[1].Enable   true
    table.IntelliTour[1].TourPlan ...

Two rows for a dual sensor camera that Home Assistant shows as two channels,
panorama first and PTZ second, so `IntelliTour` is indexed by **channel** and
not by plan. Three consequences, each with a test here:

* the switch belongs to the channel whose row carries `Enable`, so the
  panorama channel gets none and a recorder with two PTZ cameras gets two
  switches rather than one writing into the wrong row;
* the probe judges on `table.IntelliTour[<this channel>].Enable` being
  present. Judging on the read succeeding gives every device a switch, because
  `async_get_config` turns a refusal into `{}`; judging on the table being
  non-empty gives the panorama channel one, because row [0] is `null` and the
  table is one table for both;
* the poll keeps that one key. The rest of the row is the plan itself, which no
  entity reads, and on a recorder the table carries every other channel's row.

Reintroduced one at a time against the real source:

| reintroduced | caught by |
| --- | --- |
| the probe judging on the read not raising | `a_camera_without_the_table_gets_no_switch` |
| the probe judging on the table being non-empty | `the_channel_without_a_row_gets_no_switch` |
| the probe reading channel 0's key for every channel | `each_channel_judges_on_its_own_row` |
| the whole table returned from the poll | `the_poll_keeps_only_this_channels_flag` |
| the registry entry removed | `a_tour_that_arrives_late_gates_the_switch` |
| the write naming the table or the wrong row | `the_write_addresses_one_row` |
| the RPC2 write leaving the read cache alone | `the_rpc2_write_forgets_the_cached_read` |
"""

from types import SimpleNamespace

import pytest
from aiohttp import ClientResponseError
from homeassistant.const import EntityCategory

from custom_components.dahua import DahuaDataUpdateCoordinator
from custom_components.dahua import coordinator as coordinator_module
from custom_components.dahua import switch as switch_module
from custom_components.dahua.client import flatten_rpc2_config
from custom_components.dahua.const import BACKED_UP_TABLES
from custom_components.dahua.coordinator import (
    INTELLI_TOUR_ENABLE,
    RETRYABLE_CAPABILITIES,
)

from . import adds_entities

ADDRESS = "192.168.1.64"
SERIAL = "7K03D9DPAZB1F42"

# What the SDT6E432 answers, flattened as the integration reads it. Row 0 is the
# panorama sensor and carries nothing; row 1 is the PTZ sensor and carries the
# tour. The fields beside Enable are here because the poll must discard them.
TOUR_TABLE = {
    "table.IntelliTour[1].Enable": "true",
    "table.IntelliTour[1].IdleWaitingTime": "60",
    "table.IntelliTour[1].SceneSwitchingTime": "15",
    "table.IntelliTour[1].TourPriMode": "0",
    "table.IntelliTour[1].TourPlan[0].PresetId": "3",
}
TOUR_STOPPED = dict(TOUR_TABLE, **{"table.IntelliTour[1].Enable": "false"})

# A camera with no tour at all. The refusal arrives through async_get_config
# as this, which is the same thing an empty table looks like.
NO_TABLE: dict = {}


class _Device:
    """The one read this capability comes from, as the test dictates.

    The list is consumed in order and the last entry repeats, so a test says
    "silent, then it answers" without counting calls.
    """

    def __init__(self, tour=(TOUR_TABLE,)):
        self._answers = list(tour)
        self.reads = 0
        self.writes = []

    async def async_get_intelli_tour(self):
        self.reads += 1
        answer = self._answers[0] if len(self._answers) == 1 else self._answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return dict(answer)

    async def async_set_intelli_tour(self, channel, enabled):
        self.writes.append((channel, enabled))


def _coordinator(channel=1, **answers):
    """A coordinator carrying the probe, the retry machinery and nothing else.

    `object.__new__` rather than the constructor, as the rest of this suite
    does: building a real one wants a hass, an entry and a device.
    """
    coordinator = object.__new__(DahuaDataUpdateCoordinator)
    coordinator.client = _Device(**answers)
    coordinator._channel = channel
    coordinator._address = ADDRESS
    coordinator._supports_intelli_tour = False
    coordinator._probe_refusals = {}
    coordinator._capability_retries = {}
    coordinator.data = {}
    coordinator.hass = None
    coordinator.config_entry = type("E", (), {"entry_id": "entry-1"})()
    coordinator.get_serial_number = lambda: SERIAL
    return coordinator


@pytest.fixture
def discoveries(monkeypatch):
    """What the coordinator announced, captured where it looks the name up.

    `coordinator.py` does `from .late import async_notify_capability_discovered`,
    so patching `late` would not reach the bound name and the real dispatcher
    would be called with `hass=None`.
    """
    announced = []
    monkeypatch.setattr(
        coordinator_module,
        "async_notify_capability_discovered",
        lambda hass, entry_id, channel_key: announced.append((entry_id, channel_key)),
    )
    return announced


# --- what the probe concludes ------------------------------------------------


async def test_a_camera_without_the_table_gets_no_switch():
    """`async_get_config` turns a refusal into `{}`, so a probe written as
    "did the read raise" concludes that every camera has a tour."""
    coordinator = _coordinator(tour=(NO_TABLE,))

    assert await coordinator._async_probe_intelli_tour() is False
    assert coordinator.supports_intelli_tour() is False


async def test_the_channel_with_the_row_gets_the_switch():
    coordinator = _coordinator(channel=1)

    assert await coordinator._async_probe_intelli_tour() is True
    assert coordinator.supports_intelli_tour() is True


async def test_the_channel_without_a_row_gets_no_switch():
    """The neighbouring-but-wrong case, and the whole reason the probe reads a
    key rather than a table. Channel 0 of the measured camera asks for the same
    table and gets the same non-empty answer, because there is one table for
    both sensors and only row 1 is in it."""
    coordinator = _coordinator(channel=0)

    assert await coordinator._async_probe_intelli_tour() is False
    assert coordinator.supports_intelli_tour() is False


async def test_each_channel_judges_on_its_own_row():
    """A recorder with the tour on channel 7 and nothing on channel 6. A probe
    that read index 0, or the first row it found, would be right about neither."""
    table = {
        "table.IntelliTour[7].Enable": "true",
        "table.IntelliTour[7].TourPriMode": "0",
    }
    has = _coordinator(channel=7, tour=(table,))
    has_not = _coordinator(channel=6, tour=(table,))

    assert await has._async_probe_intelli_tour() is True
    assert await has_not._async_probe_intelli_tour() is False


async def test_a_stopped_tour_is_still_a_tour():
    """Enable=false is a camera with a plan that is not running, which is
    exactly when someone wants the switch."""
    coordinator = _coordinator(tour=(TOUR_STOPPED,))

    assert await coordinator._async_probe_intelli_tour() is True


async def test_an_answer_is_not_asked_for_again():
    """Both answers are answers. A camera that reports no row has said
    something, and the retry is for silence."""
    for answer in (TOUR_TABLE, NO_TABLE):
        coordinator = _coordinator(tour=(answer,))
        await coordinator._async_probe_intelli_tour()
        assert coordinator.probes_that_did_not_answer() == set()


async def test_silence_leaves_the_question_open():
    coordinator = _coordinator(tour=(TimeoutError(),))

    assert await coordinator._async_probe_intelli_tour() is False
    assert coordinator.probes_that_did_not_answer() == {"intelli_tour"}


async def test_a_refusal_is_an_answer_rather_than_a_failure():
    """A 400 is the camera declining the table, not failing to speak. Setup
    must finish either way."""
    coordinator = _coordinator(tour=(ClientResponseError(None, None, status=400),))

    assert await coordinator._async_probe_intelli_tour() is False
    assert coordinator.supports_intelli_tour() is False


async def test_the_whole_table_is_read_once_rather_than_one_row_per_channel():
    """The read is shared and cached per device, so asking for the table costs
    one round trip for every channel. Asking per row would cost sixteen."""
    coordinator = _coordinator()

    await coordinator._async_probe_intelli_tour()

    assert coordinator.client.reads == 1


# --- the measured RPC2 payload -----------------------------------------------


def test_the_null_row_flattens_to_nothing_and_keeps_the_index():
    """The shape @MassakerGU posted, through the real flattener. A null row
    must not fabricate a key for channel 0, and must not shift channel 1 up."""
    flat = flatten_rpc2_config(
        "IntelliTour",
        [None, {"Enable": True, "IdleWaitingTime": 60, "TourPriMode": 0}],
    )

    assert flat["table.IntelliTour[1].Enable"] == "true"
    assert not [key for key in flat if key.startswith("table.IntelliTour[0]")]


async def test_a_device_answering_over_rpc2_is_read_the_same_way():
    """The probe reads flat keys, so the transport that produced them cannot
    matter. This is the RPC2 answer run through the probe's own judgement."""
    flat = flatten_rpc2_config("IntelliTour", [None, {"Enable": False}])

    assert await _coordinator(channel=1, tour=(flat,))._async_probe_intelli_tour()
    assert not await _coordinator(channel=0, tour=(flat,))._async_probe_intelli_tour()


# --- the retry ---------------------------------------------------------------


def test_the_capability_is_in_the_retry_registry():
    assert "intelli_tour" in RETRYABLE_CAPABILITIES


async def test_the_name_the_probe_records_is_the_name_the_registry_is_keyed_by():
    """Two literals in two files, and nothing else ties them together. A probe
    recording "intellitour" against a registry keyed "intelli_tour" leaves a
    question marked unanswered that the retry can never pick up, and says
    nothing about it. That is what the system load probe did (#1057).

    Read from the refusal the probe actually recorded, rather than from the
    literal, so a rename on one side alone fails here."""
    coordinator = _coordinator(tour=(TimeoutError(),))
    await coordinator._async_probe_intelli_tour()

    unanswered = coordinator.probes_that_did_not_answer()

    assert unanswered, "nothing was recorded, so the retry has nothing to find"
    assert unanswered <= set(RETRYABLE_CAPABILITIES)


async def test_a_tour_that_arrives_late_gates_the_switch(discoveries):
    """Through the real registry rather than a narrowed one: a test that
    monkeypatches `RETRYABLE_CAPABILITIES` down to this capability passes just
    as happily when the registry entry is deleted."""
    coordinator = _coordinator(tour=(TimeoutError(), TOUR_TABLE))
    await coordinator._async_probe_intelli_tour()
    assert coordinator.supports_intelli_tour() is False

    await coordinator._async_retry_unanswered_capabilities()

    assert coordinator.supports_intelli_tour() is True
    assert discoveries, "the platform is never told, so no switch is created"


async def test_a_capability_already_known_is_not_announced_again(discoveries):
    coordinator = _coordinator()
    await coordinator._async_probe_intelli_tour()

    await coordinator._async_retry_unanswered_capabilities()

    assert discoveries == []


# --- the poll ----------------------------------------------------------------


async def test_the_poll_keeps_only_this_channels_flag():
    """The rest of the row is the plan: its presets, its dwell times, its
    priority. No entity reads any of it, and everything in the fan-out is
    merged into `data`, which the diagnostics download dumps."""
    coordinator = _coordinator(channel=1)

    fetched = await coordinator._async_fetch_intelli_tour()

    assert fetched == {"table.IntelliTour[1].Enable": "true"}


async def test_the_poll_does_not_carry_other_channels_rows():
    """On a recorder the table holds every channel. Returning it whole would
    put all sixteen rows into each of sixteen coordinators."""
    table = {
        "table.IntelliTour[3].Enable": "false",
        "table.IntelliTour[7].Enable": "true",
    }
    coordinator = _coordinator(channel=7, tour=(table,))

    fetched = await coordinator._async_fetch_intelli_tour()

    assert fetched == {"table.IntelliTour[7].Enable": "true"}


async def test_a_read_that_says_nothing_carries_the_previous_answer():
    """A momentary refusal must not report a running tour as stopped."""
    coordinator = _coordinator(tour=(TOUR_TABLE, TimeoutError()))
    coordinator.data = await coordinator._async_fetch_intelli_tour()

    fetched = await coordinator._async_fetch_intelli_tour()

    assert fetched == {"table.IntelliTour[1].Enable": "true"}


async def test_a_refused_read_does_not_fail_the_refresh():
    """The fan-out has no `return_exceptions`, so an unwrapped read failing
    takes every entity on the channel with it (#1006, #1024)."""
    coordinator = _coordinator(tour=(ClientResponseError(None, None, status=500),))

    assert await coordinator._async_fetch_intelli_tour() is None


# --- the entity --------------------------------------------------------------


def _switch(enabled, channel=1):
    entity = object.__new__(switch_module.DahuaIntelliTourSwitch)
    coordinator = _coordinator(channel=channel)
    coordinator.data = {INTELLI_TOUR_ENABLE.format(channel): enabled}
    entity._coordinator = coordinator
    entity.coordinator = coordinator
    return entity


def test_the_switch_reads_the_key_the_probe_judged_on():
    assert _switch("true").is_on is True
    assert _switch("false").is_on is False


def test_the_switch_reads_its_own_channels_key():
    """Channel 1's switch asked channel 0's row and read a tour that is not
    its own. The fixture gives the two different values so it cannot pass by
    coincidence."""
    entity = _switch("true", channel=1)
    entity._coordinator.data = {
        "table.IntelliTour[0].Enable": "false",
        "table.IntelliTour[1].Enable": "true",
    }

    assert entity.is_on is True


def test_the_switch_is_configuration_rather_than_a_control():
    assert _switch("true").entity_category is EntityCategory.CONFIG


def test_the_switch_names_itself_through_the_translation_file():
    assert _switch("true").translation_key == "intelli_tour"


def test_the_unique_id_is_the_serial_and_the_key():
    assert _switch("true").unique_id == SERIAL + "_intelli_tour"


async def _done(*_args, **_kwargs):
    return None


async def test_turning_it_off_writes_false_for_this_channel_and_refreshes():
    entity = _switch("true", channel=1)
    refreshed = []
    entity._coordinator.get_channel = lambda: 1
    entity._coordinator.async_refresh = lambda: refreshed.append(True) or _done()

    await entity.async_turn_off()

    assert entity._coordinator.client.writes == [(1, False)]
    assert refreshed == [True]


async def test_turning_it_on_writes_true_for_this_channel():
    entity = _switch("false", channel=7)
    entity._coordinator.get_channel = lambda: 7
    entity._coordinator.async_refresh = _done

    await entity.async_turn_on()

    assert entity._coordinator.client.writes == [(7, True)]


# --- what goes on the wire ---------------------------------------------------


class _Recording:
    def __init__(self, cgi_status=None):
        self.urls = []
        self.fields = []
        self.cleared = []
        self._cgi_status = cgi_status
        self._address = ADDRESS

    async def get(self, url, verify_ok=False):
        self.urls.append((url, verify_ok))
        if self._cgi_status is not None:
            raise ClientResponseError(None, None, status=self._cgi_status)
        return {}

    async def _rpc2_set_config_field(self, name, value):
        self.fields.append((name, value))
        return {}


def _client(cgi_status=None):
    from custom_components.dahua.client import DahuaClient

    client = object.__new__(DahuaClient)
    recording = _Recording(cgi_status)
    client.get = recording.get
    client._rpc2_set_config_field = recording._rpc2_set_config_field
    client._address = ADDRESS
    return client, recording


async def test_the_write_addresses_one_row():
    """Exactly the URL @MassakerGU measured. Not the table it sits in: that
    carries the tour's whole plan, and putting it back is how a switch that
    only meant to start the tour silently rewrites its presets."""
    client, recording = _client()

    await client.async_set_intelli_tour(1, True)

    url, verify_ok = recording.urls[0]
    assert "action=setConfig&IntelliTour[1].Enable=true" in url
    assert "TourPlan" not in url
    assert verify_ok, "a 200 with the body Error is a refusal, and must be read"


async def test_the_write_says_false_for_off():
    client, recording = _client()

    await client.async_set_intelli_tour(1, False)

    assert recording.urls[0][0].endswith("IntelliTour[1].Enable=false")


async def test_the_write_carries_the_channel_it_was_given():
    """The defect this exists for is a hard coded [1], which is the index on
    the one camera that reported, and wrong on every recorder."""
    client, recording = _client()

    await client.async_set_intelli_tour(9, True)

    assert "IntelliTour[9].Enable=true" in recording.urls[0][0]


async def test_a_device_with_no_cgi_config_path_is_written_over_rpc2():
    """An SL300 answers 404 to every /cgi-bin/ path it has. The read already
    falls back to RPC2, so the write has to as well or the switch is one way.
    The field name is the one measured over RPC2 on the SDT6E432."""
    client, recording = _client(cgi_status=404)

    await client.async_set_intelli_tour(1, False)

    assert recording.fields == [("IntelliTour[1].Enable", False)]


async def test_another_cgi_error_is_not_turned_into_an_rpc2_write():
    """A 403 is the account being refused, which no other transport fixes."""
    client, recording = _client(cgi_status=403)

    with pytest.raises(ClientResponseError):
        await client.async_set_intelli_tour(1, False)

    assert recording.fields == []


async def test_the_rpc2_write_forgets_the_cached_read(monkeypatch):
    """A device written this way serves no CGI config path, so its `getConfig`
    was answered over RPC2 and cached under the CGI URL for five minutes. The
    switch writes, refreshes, and reads back the value from before the write.
    The CGI write path clears the cache; this one has to as well."""
    from custom_components.dahua import client as client_module

    client = object.__new__(client_module.DahuaClient)
    client._device = "%s:80" % ADDRESS
    client._address = ADDRESS

    async def shared_call(action):
        return await action(SimpleNamespace(set_config_field=_done))

    client._rpc2_shared_call = shared_call
    cleared = []
    monkeypatch.setattr(client_module, "clear_host_cache", cleared.append)

    await client._rpc2_set_config_field("IntelliTour[1].Enable", True)

    assert cleared == ["%s:80" % ADDRESS]


# --- what the backup carries -------------------------------------------------


def test_the_table_is_backed_up():
    """The switch writes it, so a restore has to be able to put it back. The
    guard in test_the_backup_covers_what_can_be_changed.py scans for this, and
    this names it so the reason is readable from here too."""
    assert "IntelliTour" in BACKED_UP_TABLES


# --- one switch per channel, on the channel that has the tour ----------------


class _PlatformCoordinator:
    """What switch.py asks a coordinator while deciding what to create."""

    subentry_id = None

    def __init__(self, channel, tour):
        self._channel = channel
        self._tour = tour
        self.data = {INTELLI_TOUR_ENABLE.format(channel): "true"}
        self.client = _Device()

    def get_channel(self):
        return self._channel

    def get_serial_number(self):
        return "%s_%d" % (SERIAL, self._channel)

    def supports_intelli_tour(self):
        return self._tour

    def supports_cloud_connection(self):
        return False

    def uses_separate_host_device(self):
        return False

    def is_indoor_monitor_without_video(self):
        return False

    def creates_siren_entity(self):
        return False

    def uses_recorder_deterrence(self):
        return False

    def supports_smart_motion_detection(self):
        return False

    def supports_smart_motion_detection_amcrest(self):
        return False

    def supports_privacy_mode(self):
        return False

    def supports_alarm_output(self):
        return False

    def supports_disarming_linkage(self):
        return False

    def supports_event_notifications(self):
        return False

    def supports_vth_phone_setting(self, field):
        """Not an indoor monitor, so it has no call settings (#949)."""
        return False

    def get_ivs_rules(self):
        return []


def _entry(coordinators):
    return type(
        "E",
        (),
        {
            "entry_id": "entry-1",
            "runtime_data": dict(enumerate(coordinators)),
            "async_on_unload": staticmethod(lambda unsub: None),
        },
    )()


async def _tour_switches(coordinators):
    added = []
    await switch_module.async_setup_entry(
        SimpleNamespace(data={}), _entry(coordinators), adds_entities(added)
    )
    return [
        entity
        for entity in added
        if isinstance(entity, switch_module.DahuaIntelliTourSwitch)
    ]


async def test_only_the_channel_with_the_tour_gets_one():
    """The measured camera: panorama on channel 0, PTZ on channel 1."""
    switches = await _tour_switches(
        [_PlatformCoordinator(0, tour=False), _PlatformCoordinator(1, tour=True)]
    )

    assert len(switches) == 1
    assert switches[0].unique_id == "%s_1_intelli_tour" % SERIAL


async def test_two_ptz_cameras_on_one_recorder_get_two():
    """And they are distinct entities, not one writing into both rows."""
    switches = await _tour_switches(
        [
            _PlatformCoordinator(0, tour=False),
            _PlatformCoordinator(1, tour=True),
            _PlatformCoordinator(2, tour=False),
            _PlatformCoordinator(3, tour=True),
        ]
    )

    assert len({entity.unique_id for entity in switches}) == 2


async def test_a_camera_with_no_tour_gets_none():
    switches = await _tour_switches([_PlatformCoordinator(0, tour=False)])

    assert switches == []
