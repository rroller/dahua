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

from aiohttp import ClientResponseError

import aiohttp
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


async def _probe(answer, options=None, monitor=True):
    """Run the real probe against a client that answers `answer`.

    The client models **both** readers, the strict one the probe uses and the
    lenient sibling beside it, because they share a URL and differ only in what
    a refusal does. Without the lenient one, swapping the probe back to it
    would fail on a missing attribute, which proves nothing about behaviour --
    the trap that hid the finding in #1213.
    """
    from custom_components.dahua.coordinator import DahuaDataUpdateCoordinator

    coordinator = DahuaDataUpdateCoordinator.__new__(DahuaDataUpdateCoordinator)
    coordinator._address = "10.0.0.230"
    coordinator._probe_refusals = {}
    coordinator._capability_retries = {}
    coordinator.is_indoor_monitor = lambda: monitor
    coordinator.config_entry = SimpleNamespace(
        entry_id="entry-1", options=options or {}
    )

    async def strict():
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def lenient():
        try:
            return await strict()
        except ClientResponseError:
            return {}

    coordinator.client = SimpleNamespace(
        async_probe_video_talk_phone_general=strict,
        async_get_video_talk_phone_general=lenient,
    )
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


# --- a refused poll must not blank the monitor ------------------------------
#
# The same shape @rroller found on #1201. `async_get_config` turns an HTTP 400,
# 403 or 500 into `{}`, so the poll wrapper's handler never ran, it returned an
# empty table, and the next data replacement dropped every key the switch and
# the four volumes read. The monitor went unknown on one refused read with no
# exception anywhere.
#
# Driven through the real DahuaClient against a session that answers a status,
# not a stub that raises, because a stub skips the conversion that caused this.


class _Refused:
    """One status, to everything asked of it."""

    def __init__(self, status):
        self.status = status
        self.headers = {}

    def raise_for_status(self):
        if self.status >= 400:
            raise aiohttp.ClientResponseError(
                request_info=SimpleNamespace(real_url="http://10.0.0.230/x"),
                history=(),
                status=self.status,
            )

    async def text(self):
        return ""

    async def read(self):
        return b""

    def close(self):
        return None


class _RefusingSession:
    def __init__(self, status):
        self.status = status
        self.calls = 0

    async def request(self, method, url, headers=None, **kwargs):
        self.calls += 1
        self.url = url
        return _Refused(self.status)


def _polling_coordinator(flat, address):
    """A real coordinator, enough of one to run the poll wrapper."""
    from custom_components.dahua.coordinator import DahuaDataUpdateCoordinator

    coordinator = object.__new__(DahuaDataUpdateCoordinator)
    coordinator.data = dict(flat)
    coordinator._channel = 0
    coordinator._address = address
    return coordinator


@pytest.mark.parametrize("status", [400, 403, 500])
async def test_a_refused_poll_keeps_the_call_settings(status):
    session = _RefusingSession(status)
    coordinator = _polling_coordinator(_flat(), "10.0.0.230")
    coordinator.client = DahuaClient("u", "p", "10.0.0.230", 80, 554, session)

    carried = await coordinator._async_fetch_vth_phone_settings()

    assert session.calls, "the real client was never reached"
    assert carried, "a refused read dropped the monitor's call settings"
    assert carried["table.VideoTalkPhoneGeneral.RingVolume"] == "70"
    assert carried["table.VideoTalkPhoneGeneral.SilenceEnable"] == "false"


async def test_the_volume_still_reads_after_a_refused_poll():
    """The consequence as the user sees it: the slider keeps its value."""
    session = _RefusingSession(500)
    coordinator = _polling_coordinator(_flat(), "10.0.0.231")
    coordinator.client = DahuaClient("u", "p", "10.0.0.231", 80, 554, session)

    carried = await coordinator._async_fetch_vth_phone_settings()

    assert _coordinator(carried).get_vth_phone_number("RingVolume") == 70


async def test_an_empty_answer_is_not_mistaken_for_a_refusal():
    """The control. A monitor that answers an empty table has said something,
    and it must stay distinguishable from one that refused."""
    session = _RefusingSession(200)
    coordinator = _polling_coordinator(_flat(), "10.0.0.232")
    coordinator.client = DahuaClient("u", "p", "10.0.0.232", 80, 554, session)

    answered = await coordinator._async_fetch_vth_phone_settings()

    assert answered == {}, "an empty 200 was treated as a refusal"


async def test_the_poll_reads_the_table_by_name():
    """The URL, because a getConfig for a name the device does not have answers
    200 with an empty body, so a misspelled table reads as a monitor without
    the feature for ever."""
    session = _RefusingSession(200)
    coordinator = _polling_coordinator({}, "10.0.0.233")
    coordinator.client = DahuaClient("u", "p", "10.0.0.233", 80, 554, session)

    await coordinator._async_fetch_vth_phone_settings()

    assert "action=getConfig&name=VideoTalkPhoneGeneral" in session.url


# --- and asked again when the monitor did not answer --------------------------
#
# The poll reads this table only where fields were found:
#
#     if getattr(self, "_vth_phone_fields", frozenset()) and self._wanted_by(...)
#
# so an unanswered probe closed the loop for the life of the entry. Nothing
# asked again, and the do not disturb switch and the four volumes never
# existed. The probe recorded nothing either, so even the dump was silent.
#
# A monitor is the device most likely to miss the question. It is the one
# answering over DHIP (#949), where one session takes one call at a time, and
# setup asks it several things at once.


def _refused(status=403):
    return ClientResponseError(None, (), status=status)


async def test_a_monitor_that_did_not_answer_is_asked_again():
    """The point of this section. The switch and the volumes exist from the
    next poll rather than never."""
    from custom_components.dahua.coordinator import DahuaDataUpdateCoordinator

    found, fields = await _probe(TimeoutError())
    assert found is False
    assert fields == frozenset()

    coordinator = DahuaDataUpdateCoordinator.__new__(DahuaDataUpdateCoordinator)
    coordinator._address = "10.0.0.230"
    coordinator._probe_refusals = {"vth_phone_settings": {"answered": False}}
    coordinator._capability_retries = {}
    coordinator.is_indoor_monitor = lambda: True
    coordinator.config_entry = SimpleNamespace(entry_id="entry-1", options={})

    async def answers():
        return _flat()

    coordinator.client = SimpleNamespace(
        async_probe_video_talk_phone_general=answers,
        async_get_video_talk_phone_general=answers,
    )

    assert await coordinator._async_retry_vth_phone_settings() is True
    assert coordinator._vth_phone_fields == frozenset(VTH_PHONE_SETTINGS)
    # And the question is closed. Without this the monitor stays on the
    # unanswered list, is re-asked until the retry budget runs out, and the
    # dump goes on saying it never answered after it has. Nothing else here
    # notices, because the fields are already right by then.
    assert coordinator.probes_that_did_not_answer() == set()


async def test_silence_is_recorded_as_unanswered():
    """What the retry registry reads. A timeout is the monitor not answering,
    which is a fact about that moment and generalises to nothing, so it is the
    shape that earns another question."""
    from custom_components.dahua.coordinator import DahuaDataUpdateCoordinator

    coordinator = DahuaDataUpdateCoordinator.__new__(DahuaDataUpdateCoordinator)
    coordinator._address = "10.0.0.230"
    coordinator._probe_refusals = {}
    coordinator.is_indoor_monitor = lambda: True
    coordinator.config_entry = SimpleNamespace(entry_id="entry-1", options={})

    async def refuse():
        raise TimeoutError()

    coordinator.client = SimpleNamespace(
        async_probe_video_talk_phone_general=refuse,
        async_get_video_talk_phone_general=refuse,
    )

    await coordinator._async_probe_vth_phone_settings()

    assert coordinator.probes_that_did_not_answer() == {"vth_phone_settings"}


async def test_a_refusal_is_the_monitor_answering():
    """⭐ The assertion the strict reader is for, and the only one here that
    separates it from the lenient sibling. Through `async_get_config` a 403
    came back as `{}`, `_note_probe_answered` popped the record, and nothing
    anywhere said the monitor refused or with what status. Both readers stop
    the retry, which is right; only this one says why."""
    from custom_components.dahua.coordinator import DahuaDataUpdateCoordinator

    coordinator = DahuaDataUpdateCoordinator.__new__(DahuaDataUpdateCoordinator)
    coordinator._address = "10.0.0.230"
    coordinator._probe_refusals = {}
    coordinator.is_indoor_monitor = lambda: True
    coordinator.config_entry = SimpleNamespace(entry_id="entry-1", options={})

    async def refuse():
        raise _refused()

    async def lenient():
        try:
            return await refuse()
        except ClientResponseError:
            return {}

    coordinator.client = SimpleNamespace(
        async_probe_video_talk_phone_general=refuse,
        async_get_video_talk_phone_general=lenient,
    )

    await coordinator._async_probe_vth_phone_settings()

    assert coordinator.probes_that_did_not_answer() == set()
    assert coordinator._probe_refusals["vth_phone_settings"]["status"] == 403


async def test_a_monitor_already_known_is_not_announced():
    """A retry that finds what it already had must not report a discovery, or
    every poll rebuilds the platforms."""
    from custom_components.dahua.coordinator import DahuaDataUpdateCoordinator

    coordinator = DahuaDataUpdateCoordinator.__new__(DahuaDataUpdateCoordinator)
    coordinator._address = "10.0.0.230"
    coordinator._probe_refusals = {}
    coordinator.is_indoor_monitor = lambda: True
    coordinator.config_entry = SimpleNamespace(entry_id="entry-1", options={})

    async def answers():
        return _flat()

    coordinator.client = SimpleNamespace(
        async_probe_video_talk_phone_general=answers,
        async_get_video_talk_phone_general=answers,
    )

    await coordinator._async_probe_vth_phone_settings()

    assert await coordinator._async_retry_vth_phone_settings() is False


async def test_a_camera_is_not_asked_for_call_settings():
    """The identity gate, inside the probe so the retry applies it too. A
    camera has no call settings and the request buys nothing."""
    found, fields = await _probe(_flat(), monitor=False)

    assert found is False
    assert fields == frozenset()


async def test_a_monitor_with_neither_platform_is_not_asked():
    """The platform gate, for the same reason. Neither the switch nor the
    volumes exist with both off, and the poll already applies this one."""
    found, _fields = await _probe(_flat(), options={"switch": False, "number": False})

    assert found is False


async def test_one_platform_is_enough():
    found, _fields = await _probe(_flat(), options={"switch": False})

    assert found is True


async def test_the_generic_retry_asks_and_announces(monkeypatch):
    """Through `_async_retry_unanswered_capabilities` and the real registry,
    not by calling the retry directly: calling it directly passes just as
    happily when the registry entry is deleted, and nothing else reaches it.

    The announcement is what tells the platforms to build. Without it the
    fields are right and no entity is created until a reload."""
    from custom_components.dahua import coordinator as coordinator_module
    from custom_components.dahua.coordinator import DahuaDataUpdateCoordinator

    announced = []
    monkeypatch.setattr(
        coordinator_module,
        "async_notify_capability_discovered",
        lambda hass, entry_id, channel_key: announced.append((entry_id, channel_key)),
    )

    coordinator = DahuaDataUpdateCoordinator.__new__(DahuaDataUpdateCoordinator)
    coordinator._address = "10.0.0.230"
    coordinator._channel = 0
    coordinator._probe_refusals = {}
    coordinator._capability_retries = {}
    coordinator.hass = None
    coordinator.is_indoor_monitor = lambda: True
    coordinator.config_entry = SimpleNamespace(entry_id="entry-1", options={})
    coordinator.get_serial_number = lambda: "VTHSERIAL"

    answers = [TimeoutError(), _flat()]

    async def read():
        answer = answers.pop(0) if len(answers) > 1 else answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer

    coordinator.client = SimpleNamespace(
        async_probe_video_talk_phone_general=read,
        async_get_video_talk_phone_general=read,
    )

    await coordinator._async_probe_vth_phone_settings()
    assert coordinator.probes_that_did_not_answer() == {"vth_phone_settings"}

    await coordinator._async_retry_unanswered_capabilities()

    assert coordinator._vth_phone_fields == frozenset(VTH_PHONE_SETTINGS)
    assert coordinator.probes_that_did_not_answer() == set()
    assert announced == [("entry-1", "VTHSERIAL")], (
        "the platforms were never told, so the switch and the volumes would "
        "wait for a reload: %s" % (announced,)
    )


def test_the_retry_registry_carries_the_monitor():
    """The registration itself. Without it the probe records a refusal nothing
    ever reads, which is where this started."""
    from custom_components.dahua.coordinator import RETRYABLE_CAPABILITIES

    assert "vth_phone_settings" in RETRYABLE_CAPABILITIES


# --- and the platforms build them when it arrives ------------------------------
#
# The retry filling `_vth_phone_fields` is only half the claim. What creates
# the switch and the volumes is `async_notify_capability_discovered`, which
# re-runs each platform's `build`, and that only reaches these entities if the
# gate is read **inside** the build rather than once at setup. Everything above
# this line drives the coordinator and would pass either way.


class _No:
    """No, in every shape a platform build might ask the question."""

    def __call__(self, *args, **kwargs):
        return False

    def __iter__(self):
        return iter(())

    def __bool__(self):
        return False


class _AnythingElseIsOff:
    """A coordinator that answers no to every capability but this one.

    Permissive by `__getattr__` rather than by listing what the two platforms
    ask, because the list is long, changes often, and is not what is under
    test here.
    """

    subentry_id = None

    def __init__(self, fields=frozenset()):
        self._fields = frozenset(fields)
        self.data = {}
        self.model = "VTH5221D"

    def supports_vth_phone_setting(self, field):
        return field in self._fields

    def get_serial_number(self):
        return "VTHSERIAL"

    def get_device_name(self):
        return "Hallway"

    def get_address(self):
        return "10.0.0.230"

    def get_channel(self):
        return 0

    def get_event_list(self):
        return []

    def get_ivs_rules(self):
        return []

    def __getattr__(self, name):
        """One answer for anything not spelled out above.

        Callable, iterable, empty and false at once, because a platform's build
        reads both methods and plain collections off a coordinator and nothing
        here can tell which this name is.
        """
        if name.startswith("__"):
            raise AttributeError(name)
        return _No()


async def _builds_of(module_name, monkeypatch):
    """Every `build` the platform hands to `async_add_and_keep_watching`.

    Captured rather than driven through `late`, because what is in question is
    which callable holds the gate: `late` re-runs exactly these on a discovery,
    and a gate outside them is a gate a discovery cannot reach.
    """
    import importlib
    from types import SimpleNamespace

    module = importlib.import_module("custom_components.dahua." + module_name)
    captured = []

    async def _capture(hass, entry, build, add, coordinators, **kwargs):
        captured.append(build)

    monkeypatch.setattr(module, "async_add_and_keep_watching", _capture)
    entry = SimpleNamespace(
        entry_id="e1",
        options={},
        data={},
        subentries={},
        runtime_data={0: _AnythingElseIsOff()},
        async_on_unload=lambda unsub: None,
    )
    await module.async_setup_entry(
        SimpleNamespace(data={}), entry, lambda *a, **k: None
    )
    return captured, entry


async def _entity_names(builds, coordinator):
    names = []
    for build in builds:
        for entity in await build(coordinator) or []:
            names.append(type(entity).__name__)
    return names


@pytest.mark.parametrize(
    "module_name, entity_name",
    [("switch", "DahuaMonitorSilenceSwitch"), ("number", "DahuaMonitorVolumeNumber")],
)
async def test_a_late_answer_reaches_the_entities(
    monkeypatch, module_name, entity_name
):
    """The wiring. Build with nothing reported, then with the fields the retry
    found, and the entities appear from the same callable a discovery re-runs.
    """
    builds, _entry = await _builds_of(module_name, monkeypatch)
    assert builds, "the platform handed nothing to the late watcher"

    before = await _entity_names(builds, _AnythingElseIsOff())
    assert entity_name not in before

    after = await _entity_names(
        builds, _AnythingElseIsOff(fields=frozenset(VTH_PHONE_SETTINGS))
    )
    assert entity_name in after, (
        "a capability found on a later poll does not reach this entity, so it "
        "would need a reload rather than arriving by itself"
    )
