"""An indoor monitor's do-not-disturb switch and its volumes (#949, #998).

`VideoTalkPhoneGeneral`, measured on a VTH5221D (3.000.0012000.0.R): sixteen
keys, `SilenceEnable` a real boolean reading False, and RingVolume,
VthRingVolume, TalkVolume and AlarmVolume all 70.

**The write shape is the opposite of this integration's rule, and the reason is
in the tests below.** AGENTS.md says a write addresses one field, because a
recorder refuses a whole-table `setConfig` for size. On this monitor a
single-field write answered `result: true` and **replaced the table**, leaving
one key of sixteen and dropping the ring file and every volume. The monitor had
to be rebuilt from a copy read beforehand. So the client reads the table,
changes one key and writes it all back, and
`test_the_write_sends_the_whole_table` is the guard against anyone restoring
the usual convention here.

Both directions were then proved on the hardware: `SilenceEnable` and
`RingVolume` each took, nothing else moved, and writing the original back
restored the table byte for byte.
"""

from __future__ import annotations

import ast
import asyncio
import io
from pathlib import Path
from types import SimpleNamespace

import pytest

from custom_components.dahua.client import VTH_PHONE_TABLE, DahuaClient
from custom_components.dahua.const import (
    VTH_PHONE_SETTINGS,
    VTH_SILENCE_FIELD,
    VTH_VOLUME_FIELD_NAMES,
)
from custom_components.dahua.number import MONITOR_VOLUMES

ROOT = Path(__file__).resolve().parents[2]

# What the monitor really answered, keys and values, as the flat shape the
# coordinator sees.
MEASURED = {
    "AlarmVolume": 70,
    "AutoSnapshot": True,
    "DisableRingEndTime": "1970-01-01 00:00:00",
    "DisableRingStartTime": "1970-01-01 00:00:00",
    "LocalVideoEnable": False,
    "MaxMonitorTime": 30,
    "MaxRecordTime": 30,
    "ReviseTime": 0,
    "RingFile": "/mnt/data/Sounds/phoneRing/phone_ring1.pcm",
    "RingVolume": 70,
    "SilenceEnable": False,
    "TalkInterface": "eth0",
    "TalkVolume": 70,
    "VthRingFile": "/mnt/data/Sounds/phoneRing/phone_ring1.pcm",
    "VthRingVolume": 70,
}


def _flat(table=None):
    table = MEASURED if table is None else table
    return {
        "table.{0}.{1}".format(VTH_PHONE_TABLE, key): (
            "true" if value is True else "false" if value is False else str(value)
        )
        for key, value in table.items()
    }


# --- the two field lists cannot drift ---------------------------------------


def test_the_volume_fields_match_the_platforms_table():
    """`const` holds the device-side names, because the coordinator's probe
    reads them; `number.py` holds the same names beside their translation
    keys, because the two name scanners read those out of a literal. Derived
    rather than duplicated: a volume added to one and not the other is an
    entity with no string, or a string with no entity."""
    assert tuple(field for _key, field in MONITOR_VOLUMES) == VTH_VOLUME_FIELD_NAMES


def test_the_probe_asks_about_every_field_an_entity_reads():
    """The probe decides which entities exist, so a field no entity uses is a
    wasted question and a field it misses is an entity that never appears."""
    assert set(VTH_PHONE_SETTINGS) == {VTH_SILENCE_FIELD} | set(VTH_VOLUME_FIELD_NAMES)


# --- the write shape --------------------------------------------------------


def _client(table):
    client = DahuaClient.__new__(DahuaClient)
    client._address = "10.0.0.230"  # noqa: SLF001
    # The write clears this device's shared read cache, so the double needs
    # the key that identifies it the same way the real client does.
    client._device = "10.0.0.230:80"  # noqa: SLF001
    sent = []

    class _Rpc2:
        async def get_config(self, params):
            sent.append(("get", params))
            return {"table": dict(table)} if table is not None else {}

        async def request(self, method, params=None, **kwargs):
            sent.append((method, params))
            return {"result": True}

    async def shared(action):
        return await action(_Rpc2())

    client._rpc2_shared_call = shared  # noqa: SLF001
    return client, sent


async def test_the_write_sends_the_whole_table():
    """The regression test for the destructive write.

    A `setConfig` carrying only the changed key replaced the table on the real
    monitor, leaving one key of sixteen. Everything that was there must go
    back out with the change.
    """
    client, sent = _client(MEASURED)

    await client.async_set_video_talk_phone_field(VTH_SILENCE_FIELD, True)

    written = dict(sent[-1][1]["table"])
    assert sent[-1][0] == "configManager.setConfig"
    assert sent[-1][1]["name"] == VTH_PHONE_TABLE
    assert len(written) == len(MEASURED)
    assert written[VTH_SILENCE_FIELD] is True
    # Every other key byte for byte, which is the part a one-field write lost.
    assert {k: v for k, v in written.items() if k != VTH_SILENCE_FIELD} == {
        k: v for k, v in MEASURED.items() if k != VTH_SILENCE_FIELD
    }


async def test_the_table_is_read_again_inside_the_action():
    """`_rpc2_shared_call` may run the action twice after an expired session.
    Reading inside it means the retry writes what the device holds now, rather
    than a copy captured before the session died."""
    client, sent = _client(MEASURED)

    await client.async_set_video_talk_phone_field("RingVolume", 40)

    assert sent[0][0] == "get"
    assert sent[0][1] == {"name": VTH_PHONE_TABLE}


async def test_a_volume_is_written_as_the_number_it_is():
    client, sent = _client(MEASURED)

    await client.async_set_video_talk_phone_field("RingVolume", 40)

    assert sent[-1][1]["table"]["RingVolume"] == 40


async def test_a_field_the_table_does_not_have_is_refused():
    """A `setConfig` naming an unknown key answers OK and changes nothing, so
    the mistake would otherwise be invisible for ever."""
    client, sent = _client(MEASURED)

    with pytest.raises(ValueError, match="RingVolumeX"):
        await client.async_set_video_talk_phone_field("RingVolumeX", 40)

    assert [call for call in sent if call[0] != "get"] == []


async def test_a_device_without_the_table_is_not_written_to():
    client, sent = _client(None)

    with pytest.raises(ValueError, match=VTH_PHONE_TABLE):
        await client.async_set_video_talk_phone_field(VTH_SILENCE_FIELD, True)

    assert [call for call in sent if call[0] != "get"] == []


async def test_the_write_drops_the_cached_read():
    """Found on the hardware, and invisible to every test above.

    The read of this table is shared across the device and cached for five
    minutes, and this write does not go through `get`, which is where a write
    normally clears it. So the entity wrote the monitor, called
    `async_refresh`, was served the cached answer, and snapped straight back
    to the old value. The device had changed; the dashboard said it had not.
    """
    from custom_components.dahua import client as client_module

    cleared = []
    client, _sent = _client(MEASURED)
    original = client_module.clear_host_cache
    client_module.clear_host_cache = cleared.append
    try:
        await client.async_set_video_talk_phone_field(VTH_SILENCE_FIELD, True)
    finally:
        client_module.clear_host_cache = original

    assert cleared == ["10.0.0.230:80"]


def test_the_source_never_writes_a_single_field():
    """A reader who knows this codebase will 'fix' this back to the one-field
    convention AGENTS.md states, which is how the monitor's table was lost.
    This reads the source so the comment is not the only thing stopping them.
    """
    source = io.open(
        ROOT / "custom_components" / "dahua" / "client.py", encoding="utf-8"
    ).read()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        if node.name != "async_set_video_talk_phone_field":
            continue
        body = ast.get_source_segment(source, node) or ""
        assert "wanted = dict(table)" in body, (
            "the whole table must be copied and amended; a single-key setConfig "
            "replaces it on a VTH"
        )
        return
    raise AssertionError("async_set_video_talk_phone_field has gone")


# --- the probe and the accessors --------------------------------------------


def _coordinator(flat, fields=None):
    coordinator = SimpleNamespace(
        data=flat,
        _vth_phone_fields=frozenset(VTH_PHONE_SETTINGS if fields is None else fields),
    )
    coordinator.supports_vth_phone_setting = (
        lambda field: field in coordinator._vth_phone_fields
    )
    from custom_components.dahua.coordinator import DahuaDataUpdateCoordinator

    coordinator.get_vth_phone_number = (
        lambda field: DahuaDataUpdateCoordinator.get_vth_phone_number(
            coordinator, field
        )
    )
    coordinator.get_vth_phone_flag = (
        lambda field: DahuaDataUpdateCoordinator.get_vth_phone_flag(coordinator, field)
    )
    return coordinator


def test_a_volume_is_read_from_the_polled_table():
    assert _coordinator(_flat()).get_vth_phone_number("RingVolume") == 70


def test_a_volume_nobody_polled_is_none_rather_than_zero():
    """Zero is a volume. A slider showing silence for a value that was never
    read is worse than one that is unavailable."""
    assert _coordinator({}).get_vth_phone_number("RingVolume") is None


def test_a_neighbouring_field_is_not_answered_from_the_wrong_key():
    """RingVolume and VthRingVolume are the door station's and the monitor's
    own, and reading one for the other is the realistic defect."""
    flat = _flat(dict(MEASURED, RingVolume=70, VthRingVolume=30))
    coordinator = _coordinator(flat)

    assert coordinator.get_vth_phone_number("RingVolume") == 70
    assert coordinator.get_vth_phone_number("VthRingVolume") == 30


def test_the_flag_reads_the_word_the_flat_shape_carries():
    """`flatten_rpc2_config` turns RPC2's JSON into the same flat text a CGI
    response gives, so a real boolean arrives as the word `true`."""
    assert (
        _coordinator(_flat(dict(MEASURED, SilenceEnable=True))).get_vth_phone_flag(
            VTH_SILENCE_FIELD
        )
        is True
    )
    assert _coordinator(_flat()).get_vth_phone_flag(VTH_SILENCE_FIELD) is False


def test_the_flag_is_none_when_it_was_never_read():
    """So the switch is unavailable rather than claiming the monitor rings."""
    assert _coordinator({}).get_vth_phone_flag(VTH_SILENCE_FIELD) is None


# --- the probe decides which entities exist ---------------------------------


async def _probe(answer):
    """Run the real probe against a client that answers `answer`."""
    from custom_components.dahua.coordinator import DahuaDataUpdateCoordinator

    coordinator = DahuaDataUpdateCoordinator.__new__(DahuaDataUpdateCoordinator)
    coordinator._address = "10.0.0.230"

    async def read():
        if isinstance(answer, Exception):
            raise answer
        return answer

    coordinator.client = SimpleNamespace(async_get_video_talk_phone_general=read)
    found = await DahuaDataUpdateCoordinator._async_probe_vth_phone_settings(
        coordinator
    )
    return found, coordinator._vth_phone_fields


async def test_a_monitor_that_reports_everything_gets_everything():
    found, fields = await _probe(_flat())

    assert found is True
    assert fields == frozenset(VTH_PHONE_SETTINGS)


async def test_only_the_settings_the_device_reported_are_offered():
    """A monitor reporting four of the five should get four entities, not a
    fifth that can only ever read unknown."""
    partial = {k: v for k, v in MEASURED.items() if k != "AlarmVolume"}

    _found, fields = await _probe(_flat(partial))

    assert "AlarmVolume" not in fields
    assert VTH_SILENCE_FIELD in fields


async def test_a_device_with_no_such_table_gets_nothing():
    """`async_get_config` answers {} for a table a device does not serve, so a
    probe judging on "did it raise" would give every monitor all five."""
    found, fields = await _probe({})

    assert found is False
    assert fields == frozenset()


async def test_a_refused_read_is_not_an_entity_set():
    found, fields = await _probe(RuntimeError("declined"))

    assert found is False
    assert fields == frozenset()


async def test_a_neighbouring_table_does_not_count():
    """The probe must read its own table's keys. A flat dict carrying another
    table's `SilenceEnable` would otherwise create the switch."""
    _found, fields = await _probe({"table.SomethingElse.SilenceEnable": "false"})

    assert fields == frozenset()


async def test_concurrent_settings_writes_preserve_both_changes():
    state = dict(MEASURED)
    first, _ = _client(state)
    second, _ = _client(state)

    async def get_config(params):
        assert params == {"name": VTH_PHONE_TABLE}
        snapshot = dict(state)
        await asyncio.sleep(0)
        return {"table": snapshot}

    async def request(method, params):
        assert method == "configManager.setConfig"
        assert params["name"] == VTH_PHONE_TABLE
        state.clear()
        state.update(params["table"])
        return {"result": True}

    async def shared(action):
        return await action(SimpleNamespace(get_config=get_config, request=request))

    first._rpc2_shared_call = shared
    second._rpc2_shared_call = shared
    await asyncio.gather(
        first.async_set_video_talk_phone_field(VTH_SILENCE_FIELD, True),
        second.async_set_video_talk_phone_field("RingVolume", 40),
    )
    assert state == dict(MEASURED, SilenceEnable=True, RingVolume=40)


@pytest.mark.parametrize("raw", ["invalid", "", "2"])
def test_a_malformed_silence_flag_is_unknown(raw):
    coordinator = _coordinator({"table.VideoTalkPhoneGeneral.SilenceEnable": raw})
    assert coordinator.get_vth_phone_flag(VTH_SILENCE_FIELD) is None
