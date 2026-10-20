"""The switch for the device's own cloud connection (#591).

@ScratMan was driving `VSP_PaaS.Enable` from a `shell_command` curl, to stop a
VTO2311R-WP ringing the DMSS app while someone was home, and asked for generic
`getConfig` and `setConfig` actions to do it from the integration. The generic
version is the wrong shape -- some Dahua config writes apply one way only, and
an action that looks supported implies reversible -- but this one field is a
toggle with a known meaning that the reporter has proven reverses cleanly.

Two things measured on hardware decide the design, and both say the switch has
to be gated on the key rather than on the read succeeding:

* A VTO2311R-WP serves `VSP_PaaS` *and* `T2UServer`, and disabling T2UServer
  alone does not stop a DMSS call. They are two features, not one under two
  names, so this reads only VSP_PaaS.
* A VTO2000A answers HTTP 500 for VSP_PaaS while `General`, `AccessControl`,
  `T2UServer` and `NAS` answer 200 on the same request with the same
  credentials. `async_get_config` turns that refusal into `{}`, so a probe
  written as "did the read raise" would give that doorbell a switch which
  reads off for ever and writes into a table that is not there. That is #1068
  one table along.

The value is polled, and the table it comes from carries the device's cloud
credentials (`SN`, `Key`, `AbHash`, `Username`). Everything in the poll's
fan-out is merged into `coordinator.data`, which the diagnostics download
dumps, so the fetch keeps the one key the switch reads and discards the rest.

Reintroduced one at a time against the real source:

| reintroduced | caught by |
| --- | --- |
| the probe judging on the read not raising | `a_device_without_the_table_gets_no_switch` |
| the whole table returned from the poll | `the_poll_keeps_only_the_flag` |
| the registry entry removed | `a_table_that_arrives_late_gates_the_switch` |
| the write sending the table back | `the_write_addresses_one_field` |
| one switch per channel instead of per device | `one_switch_for_the_whole_recorder` |
"""

from types import SimpleNamespace

import pytest
from aiohttp import ClientResponseError
from homeassistant.const import EntityCategory

from custom_components.dahua import DahuaDataUpdateCoordinator
from custom_components.dahua import coordinator as coordinator_module
from custom_components.dahua import switch as switch_module
from custom_components.dahua.coordinator import (
    CLOUD_CONNECTION_ENABLE,
    RETRYABLE_CAPABILITIES,
)

from . import adds_entities

ADDRESS = "192.168.1.110"
SERIAL = "CC0000000000D2"

# What a VTO2311R-WP answers, trimmed to the fields that matter here and to
# the ones this must not keep. The real table has twenty-six.
CLOUD_TABLE = {
    "table.VSP_PaaS.Enable": "false",
    "table.VSP_PaaS.Online": "false",
    "table.VSP_PaaS.SN": "CC0000000000D2",
    "table.VSP_PaaS.AbHash": "418000000",
    "table.VSP_PaaS.DMSServerAddr": "devicehttpproxy-fk.easy4ipcloud.com",
}
CLOUD_ON = dict(CLOUD_TABLE, **{"table.VSP_PaaS.Enable": "true"})

# What a VTO2000A answers: the 500 arrives through async_get_config as {}.
NO_TABLE: dict = {}


class _Device:
    """The one read this capability comes from, as the test dictates.

    The list is consumed in order and the last entry repeats, so a test says
    "silent, then it answers" without counting calls.
    """

    def __init__(self, cloud=(CLOUD_TABLE,)):
        self._answers = list(cloud)
        self.reads = 0
        self.writes = []

    async def async_get_cloud_connection(self):
        self.reads += 1
        answer = self._answers[0] if len(self._answers) == 1 else self._answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return dict(answer)

    async def async_set_cloud_connection(self, enabled):
        self.writes.append(enabled)


def _coordinator(**answers):
    """A coordinator carrying the probe, the retry machinery and nothing else.

    `object.__new__` rather than the constructor, as the rest of this suite
    does: building a real one wants a hass, an entry and a device.
    """
    coordinator = object.__new__(DahuaDataUpdateCoordinator)
    coordinator.client = _Device(**answers)
    coordinator._channel = 0
    coordinator._address = ADDRESS
    coordinator._supports_cloud_connection = False
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


async def test_a_device_without_the_table_gets_no_switch():
    """The VTO2000A. Its 500 reaches the probe as `{}`, so the only thing that
    can tell the two apart is whether the key is in the answer."""
    coordinator = _coordinator(cloud=(NO_TABLE,))

    assert await coordinator._async_probe_cloud_connection() is False
    assert coordinator.supports_cloud_connection() is False


async def test_a_device_that_reports_the_key_gets_the_switch():
    coordinator = _coordinator()

    assert await coordinator._async_probe_cloud_connection() is True
    assert coordinator.supports_cloud_connection() is True


async def test_an_answer_is_not_asked_for_again():
    """Both answers are answers. A device that says it has no such table has
    said something, and the retry is for silence."""
    for answer in (CLOUD_TABLE, NO_TABLE):
        coordinator = _coordinator(cloud=(answer,))
        await coordinator._async_probe_cloud_connection()
        assert coordinator.probes_that_did_not_answer() == set()


async def test_silence_leaves_the_question_open():
    coordinator = _coordinator(cloud=(TimeoutError(),))

    assert await coordinator._async_probe_cloud_connection() is False
    assert coordinator.probes_that_did_not_answer() == {"cloud_connection"}


async def test_a_refusal_is_an_answer_rather_than_a_failure():
    """A 403 is the device declining the table, not failing to speak."""
    refusal = ClientResponseError(None, None, status=403)
    coordinator = _coordinator(cloud=(refusal,))

    assert await coordinator._async_probe_cloud_connection() is False
    assert coordinator.supports_cloud_connection() is False


# --- the retry ---------------------------------------------------------------


def test_the_capability_is_in_the_retry_registry():
    assert "cloud_connection" in RETRYABLE_CAPABILITIES


async def test_a_table_that_arrives_late_gates_the_switch(discoveries):
    """Through the real registry rather than a narrowed one: a test that
    monkeypatches `RETRYABLE_CAPABILITIES` down to this capability passes just
    as happily when the registry entry is deleted."""
    coordinator = _coordinator(cloud=(TimeoutError(), CLOUD_TABLE))
    await coordinator._async_probe_cloud_connection()
    assert coordinator.supports_cloud_connection() is False

    await coordinator._async_retry_unanswered_capabilities()

    assert coordinator.supports_cloud_connection() is True
    assert discoveries, "the platform is never told, so no switch is created"


async def test_a_capability_already_known_is_not_announced_again(discoveries):
    coordinator = _coordinator()
    await coordinator._async_probe_cloud_connection()

    await coordinator._async_retry_unanswered_capabilities()

    assert discoveries == []


# --- the poll ----------------------------------------------------------------


async def test_the_poll_keeps_only_the_flag():
    """The rest of that table is the device's cloud credentials and its server
    list. The poll's answers are merged into `data`, which diagnostics dumps."""
    coordinator = _coordinator()

    fetched = await coordinator._async_fetch_cloud_connection()

    assert fetched == {CLOUD_CONNECTION_ENABLE: "false"}


async def test_a_read_that_says_nothing_carries_the_previous_answer():
    """A momentary refusal must not report the cloud as switched off."""
    coordinator = _coordinator(cloud=(CLOUD_ON, TimeoutError()))
    coordinator.data = await coordinator._async_fetch_cloud_connection()

    fetched = await coordinator._async_fetch_cloud_connection()

    assert fetched == {CLOUD_CONNECTION_ENABLE: "true"}


async def test_a_refused_read_does_not_fail_the_refresh():
    """The fan-out has no `return_exceptions`, so an unwrapped read failing
    takes every entity on the channel with it (#1006, #1024)."""
    coordinator = _coordinator(cloud=(ClientResponseError(None, None, status=500),))

    assert await coordinator._async_fetch_cloud_connection() is None


# --- the entity --------------------------------------------------------------


def _switch(enabled):
    entity = object.__new__(switch_module.DahuaCloudConnectionSwitch)
    coordinator = _coordinator()
    coordinator.data = {CLOUD_CONNECTION_ENABLE: enabled}
    entity._coordinator = coordinator
    entity.coordinator = coordinator
    return entity


def test_the_switch_reads_the_key_the_probe_judged_on():
    assert _switch("true").is_on is True
    assert _switch("false").is_on is False


def test_the_switch_is_configuration_rather_than_a_control():
    assert _switch("true").entity_category is EntityCategory.CONFIG


def test_the_switch_names_itself_through_the_translation_file():
    assert _switch("true").translation_key == "cloud_connection"


def test_the_unique_id_is_the_serial_and_the_key():
    assert _switch("true").unique_id == SERIAL + "_cloud_connection"


async def test_turning_it_off_writes_false_and_refreshes():
    entity = _switch("true")
    refreshed = []
    entity._coordinator.async_refresh = lambda: refreshed.append(True) or _done()

    await entity.async_turn_off()

    assert entity._coordinator.client.writes == [False]
    assert refreshed == [True]


async def test_turning_it_on_writes_true():
    entity = _switch("false")
    entity._coordinator.async_refresh = _done

    await entity.async_turn_on()

    assert entity._coordinator.client.writes == [True]


async def _done(*_args, **_kwargs):
    return None


# --- one switch per device ---------------------------------------------------
#
# The cloud connection is the box's: VSP_PaaS is served host wide and carries
# one Enable for the whole device. A recorder is one entry with a coordinator
# per channel (#827), so without `_cloud_reporter` sixteen switches would
# fight over one flag.


def _candidate(channel, answers=True):
    return SimpleNamespace(
        supports_cloud_connection=lambda: answers,
        get_channel=lambda: channel,
    )


def test_the_lowest_answering_channel_carries_it():
    chosen = switch_module._cloud_reporter(
        [_candidate(4), _candidate(1), _candidate(9)]
    )

    assert chosen.get_channel() == 1


def test_nothing_carries_it_when_the_device_does_not_report():
    assert (
        switch_module._cloud_reporter(
            [_candidate(0, answers=False), _candidate(2, answers=False)]
        )
        is None
    )


class _PlatformCoordinator:
    """What switch.py asks a coordinator while deciding what to create."""

    subentry_id = None

    def __init__(self, channel, cloud):
        self._channel = channel
        self._cloud = cloud
        self.data = {CLOUD_CONNECTION_ENABLE: "true"}
        self.client = _Device()

    def get_channel(self):
        return self._channel

    def get_serial_number(self):
        return "%s_%d" % (SERIAL, self._channel)

    def supports_cloud_connection(self):
        return self._cloud

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

    def supports_intelli_tour(self):
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


async def test_one_switch_for_the_whole_recorder():
    added = []
    coordinators = [_PlatformCoordinator(channel, cloud=True) for channel in range(4)]

    await switch_module.async_setup_entry(
        SimpleNamespace(data={}), _entry(coordinators), adds_entities(added)
    )

    cloud = [
        entity
        for entity in added
        if isinstance(entity, switch_module.DahuaCloudConnectionSwitch)
    ]
    assert len(cloud) == 1, "one cloud connection, one switch"
    assert cloud[0].unique_id == "%s_0_cloud_connection" % SERIAL


async def test_a_device_that_refuses_the_table_gets_none():
    added = []

    await switch_module.async_setup_entry(
        SimpleNamespace(data={}),
        _entry([_PlatformCoordinator(0, cloud=False)]),
        adds_entities(added),
    )

    assert not any(
        isinstance(entity, switch_module.DahuaCloudConnectionSwitch) for entity in added
    )


# --- the write ---------------------------------------------------------------


class _Recording:
    """The real client, with only `get` and the RPC2 field write replaced."""

    def __init__(self, cgi_status=None):
        self.urls = []
        self.fields = []
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


async def test_the_write_addresses_one_field():
    """Not the table it sits in. That table carries the device's cloud
    credentials, and a whole-table write is refused for size on at least one
    recorder (errCode 287638033, "Request length error!")."""
    client, recording = _client()

    await client.async_set_cloud_connection(False)

    url, verify_ok = recording.urls[0]
    assert "action=setConfig&VSP_PaaS.Enable=false" in url
    assert "table" not in url
    assert verify_ok, "a 200 with the body Error is a refusal, and must be read"


async def test_the_write_says_true_for_on():
    client, recording = _client()

    await client.async_set_cloud_connection(True)

    assert recording.urls[0][0].endswith("VSP_PaaS.Enable=true")


async def test_a_device_with_no_cgi_config_path_is_written_over_rpc2():
    """An SL300 answers 404 to every /cgi-bin/ path it has. The read already
    falls back to RPC2, so the write has to as well or the switch is one way."""
    client, recording = _client(cgi_status=404)

    await client.async_set_cloud_connection(False)

    assert recording.fields == [("VSP_PaaS.Enable", False)]


async def test_another_cgi_error_is_not_turned_into_an_rpc2_write():
    """A 403 is the account being refused, which no other transport fixes."""
    client, recording = _client(cgi_status=403)

    with pytest.raises(ClientResponseError):
        await client.async_set_cloud_connection(False)

    assert recording.fields == []


# --- what the backup carries -------------------------------------------------
#
# The backup reads back every table the integration can write, so adding a
# write path adds this one to it. That table is not only settings: it carries
# the device's cloud key, its cloud login and its serial, and a backup is a
# file somebody attaches to a bug report.


def test_the_backup_keeps_the_setting_and_drops_the_credentials():
    from custom_components.dahua.camera import _redacted

    kept = _redacted(CLOUD_TABLE)

    assert kept["table.VSP_PaaS.Enable"] == "false", "the setting is the point"
    assert kept["table.VSP_PaaS.DMSServerAddr"].endswith("easy4ipcloud.com")
    assert kept["table.VSP_PaaS.SN"] == "REDACTED"
    assert kept["table.VSP_PaaS.AbHash"] == "REDACTED"


def test_the_redaction_is_by_field_name_not_by_table():
    """So a table added later that carries one of these is covered too."""
    from custom_components.dahua.camera import _redacted

    kept = _redacted(
        {
            "table.T2UServer.Enable": "true",
            "table.T2UServer.Key": "secret",
            "table.T2UServer.UUID": "CC0000000000D2",
        }
    )

    assert kept == {
        "table.T2UServer.Enable": "true",
        "table.T2UServer.Key": "REDACTED",
        "table.T2UServer.UUID": "REDACTED",
    }


# --- the answer that arrives late -------------------------------------------
#
# The switch was created once at setup, outside the rebuild, so a device whose
# VSP_PaaS read timed out got nothing: the retry announced the capability and
# the announcement rebuilds `build`, which did not create this entity. Found in
# review of #1074 and reproduced exactly as below.
#
# Driven through a real `hass` and the real dispatcher rather than a fake one,
# because the failure was in what the announcement reaches, and a fake
# dispatcher that calls the listener directly would have passed either way.


def _recording_add(added):
    def add(entities, update_before_add=False, *, config_subentry_id=None):
        added.extend((entity, config_subentry_id) for entity in entities)

    return add


def _cloud(added):
    return [
        (entity, where)
        for entity, where in added
        if isinstance(entity, switch_module.DahuaCloudConnectionSwitch)
    ]


async def test_a_cloud_table_that_arrives_late_creates_the_switch(hass):
    """The blocker from the #1074 review. A device too busy to answer at setup
    got no switch until somebody reloaded the entry."""
    from custom_components.dahua import late

    added = []
    coordinator = _PlatformCoordinator(0, cloud=False)
    entry = _entry([coordinator])

    await switch_module.async_setup_entry(hass, entry, _recording_add(added))
    assert _cloud(added) == []

    coordinator._cloud = True
    late.async_notify_capability_discovered(
        hass, entry.entry_id, coordinator.get_serial_number()
    )
    await hass.async_block_till_done()

    assert len(_cloud(added)) == 1, "the retry answered and nothing was created"


async def test_the_late_switch_is_filed_on_the_box(hass):
    """A recorder has a device of its own, and the cloud connection is the
    box's rather than the channel's, whether the answer arrives at setup or
    later."""
    from custom_components.dahua import late

    added = []
    coordinator = _PlatformCoordinator(0, cloud=False)
    entry = _entry([coordinator])
    await switch_module.async_setup_entry(hass, entry, _recording_add(added))

    coordinator._cloud = True
    late.async_notify_capability_discovered(
        hass, entry.entry_id, coordinator.get_serial_number()
    )
    await hass.async_block_till_done()

    assert [where for _entity, where in _cloud(added)] == [None]


async def test_a_lower_channel_answering_later_does_not_add_a_second(hass):
    """The fault #1076 was sent back for, one platform along: the serial of the
    chosen channel is part of the unique id, so a reporter that changes after a
    late answer means two switches over one flag."""
    from custom_components.dahua import late

    added = []
    channels = [
        _PlatformCoordinator(0, cloud=False),
        _PlatformCoordinator(1, cloud=True),
    ]
    entry = _entry(channels)

    await switch_module.async_setup_entry(hass, entry, _recording_add(added))
    assert len(_cloud(added)) == 1

    channels[0]._cloud = True
    late.async_notify_capability_discovered(
        hass, entry.entry_id, channels[0].get_serial_number()
    )
    await hass.async_block_till_done()

    switches = _cloud(added)
    assert len(switches) == 1, "one cloud connection, one switch"
    assert switches[0][0].unique_id == "%s_1_cloud_connection" % SERIAL


async def test_nothing_is_created_twice_on_repeated_announcements(hass):
    """`build` runs again on every announcement and returns what already
    exists; a second copy would collide on unique_id."""
    from custom_components.dahua import late

    added = []
    coordinator = _PlatformCoordinator(0, cloud=True)
    entry = _entry([coordinator])
    await switch_module.async_setup_entry(hass, entry, _recording_add(added))
    assert len(_cloud(added)) == 1

    for _ in range(3):
        late.async_notify_capability_discovered(
            hass, entry.entry_id, coordinator.get_serial_number()
        )
        await hass.async_block_till_done()

    assert len(_cloud(added)) == 1
