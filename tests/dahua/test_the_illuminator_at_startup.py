"""What the illuminator does when Home Assistant starts, and what it refuses to do.

Turning the illuminator on writes a persistent change to the camera and saves what was
there before. If Home Assistant stops between those two things, the camera keeps a
setting nobody chose and the only record of the original is that snapshot. So the entity
reads it back at startup and finishes the job.

That makes the snapshot dangerous in the other direction: writing a stale one back over a
setting the user has since chosen is exactly the harm the recovery exists to prevent.
Every path here that says "leave it alone" is protecting against that, and none of them
was executed by the suite.

Four ways a snapshot is refused, and they are not the same:

* there is no store at all, which is an entity that was never added properly;
* the record is not a dict, or is not marked active, which is a device that finished
  cleanly last time;
* the phase is a word this version does not know, which is a snapshot written by a newer
  version;
* the record is missing a field or holds the wrong type, which is corruption.

The last two are told apart on purpose. An unknown phase is raised deliberately and lands
in the same handler as corruption, so both leave the snapshot untouched rather than
guessing at it.

`async_added_to_hass` wraps the recovery in its own handler for the same reason. A
recovery that throws must not stop the entity being added, and must not destroy the
snapshot on the way out: the next restart may still need it.
"""

import pytest
from types import SimpleNamespace
from unittest.mock import Mock

from custom_components.dahua import light as light_module
from custom_components.dahua.light import DahuaIlluminator

CHANNEL = 3


class _Store:
    """The saved snapshot, and whether anything removed it."""

    def __init__(self, data=None, loads=None):
        self.data = data
        self.loads = loads
        self.removed = 0
        self.saved = []

    async def async_load(self):
        if isinstance(self.loads, BaseException):
            raise self.loads
        return self.data

    async def async_save(self, data):
        self.saved.append(data)

    async def async_remove(self):
        self.removed += 1


class _Client:
    def __init__(self):
        self.writes = []

    async def async_set_lighting_v2_raw(self, *args):
        self.writes.append(("v2_raw",) + args)

    async def async_set_lighting_scheme(self, *args):
        self.writes.append(("scheme",) + args)
        return "WhiteMode"

    async def async_get_lighting_scheme_mode(self, *args):
        return "WhiteMode"

    async def async_get_lighting_v2_live_state(self, *args):
        return ("Manual", "MiddleLight", 100)


class _Coordinator:
    def __init__(self, generation=0):
        self.client = _Client()
        self.generation = generation

    def get_camera_reboot_generation(self):
        return self.generation

    def get_serial_number(self):
        return "SERIAL1"

    def get_channel(self):
        return CHANNEL

    def get_profile_mode(self):
        return "1"

    def get_illuminator_index(self):
        return 0

    def get_illuminator_bank(self):
        return "MiddleLight"


def _light(store=None, coordinator=None):
    entity = object.__new__(DahuaIlluminator)
    entity._coordinator = coordinator or _Coordinator()
    entity.coordinator = entity._coordinator
    entity._restore_store = store
    entity._last_brightness = 255
    entity._manual_on = False
    entity._light_restore = None
    entity._scheme_restore = None
    entity._seen_reboot_generation = 0
    entity._reboot_recovery_task = None
    entity._external_change_task = None
    entity.async_write_ha_state = Mock()
    return entity


def _snapshot(**overrides):
    """A complete, valid snapshot. Tests break one field at a time."""
    data = {
        "active": True,
        "phase": "override",
        "scheme_channel": CHANNEL,
        "scheme_profile": "1",
        "previous_scheme": "WhiteMode",
        "channel": CHANNEL,
        "profile_mode": "1",
        "index": 0,
        "field": "MiddleLight",
        "old_mode": "Off",
        "old_brightness": 50,
        "ha_brightness": 255,
    }
    data.update(overrides)
    return data


# --- a snapshot that must be left alone -------------------------------------

async def test_no_store_recovers_nothing():
    """An entity that was never added properly. Recovery cannot run and must say
    so rather than raising into the caller's handler."""
    assert await _light(store=None)._recover_persisted_override() is False


@pytest.mark.parametrize("data", [None, [], "", {"active": False}, {}])
async def test_a_record_that_is_not_an_active_snapshot_is_ignored(data):
    """Nothing saved, or saved and already finished. This is the ordinary case on
    every restart of a camera whose light was never turned on through HA."""
    store = _Store(data=data)

    assert await _light(store)._recover_persisted_override() is False
    assert store.removed == 0, "a record it declined to read was deleted"


async def test_a_phase_this_version_does_not_know_is_left_untouched():
    """Written by a newer version. Guessing at it could write a stale camera state
    over whatever the newer version was in the middle of doing, so it is raised
    deliberately and lands in the corruption handler."""
    store = _Store(data=_snapshot(phase="rewriting-everything"))

    assert await _light(store)._recover_persisted_override() is False
    assert store.removed == 0


@pytest.mark.parametrize("missing", [
    "scheme_channel", "scheme_profile", "channel", "profile_mode", "index",
    "field",
])
async def test_a_snapshot_missing_a_field_is_left_untouched(missing):
    """Each of these is read without a default, so a record written by an older
    version or truncated mid-write raises KeyError. Every one is tested because the
    handler catching them says nothing about which of them can reach it."""
    data = _snapshot()
    del data[missing]
    store = _Store(data=data)

    assert await _light(store)._recover_persisted_override() is False
    assert store.removed == 0


@pytest.mark.parametrize("field, value", [
    ("channel", "not a number"),
    ("index", None),
    ("scheme_channel", []),
])
async def test_a_snapshot_holding_the_wrong_type_is_left_untouched(field, value):
    """The other half of the same handler. `int()` on a string that is not a
    number raises ValueError and on None raises TypeError, and both mean the same
    thing: this record cannot be trusted."""
    store = _Store(data=_snapshot(**{field: value}))

    assert await _light(store)._recover_persisted_override() is False
    assert store.removed == 0


async def test_a_refused_snapshot_writes_nothing_to_the_camera():
    """The point of all of the above. The camera's current state is whatever the
    user last chose, and a record we could not read is not a reason to change it."""
    coordinator = _Coordinator()
    store = _Store(data=_snapshot(phase="unknown"))

    await _light(store, coordinator)._recover_persisted_override()

    assert coordinator.client.writes == []


# --- what startup does ------------------------------------------------------

@pytest.fixture
def at_startup(monkeypatch):
    """`async_added_to_hass` calls `super()` and builds a real Store.

    `DahuaBaseEntity` does not define `async_added_to_hass`, so the call resolves
    to `CoordinatorEntity`'s, which wants the entity registered with Home
    Assistant. Setting one on the base class shadows it for the duration, which is
    scoped to that class rather than to asyncio or to Home Assistant at large.
    """
    async def _nothing(self):
        return None

    monkeypatch.setattr(
        light_module.DahuaBaseEntity, "async_added_to_hass", _nothing,
        raising=False)

    built = []

    def _store(hass, version, key):
        built.append((version, key))
        return _Store()

    monkeypatch.setattr(light_module, "Store", _store)
    return built


async def test_startup_builds_a_store_of_its_own(at_startup):
    """Keyed by entry and entity, because one recorder has a light per channel and
    a shared key would have them overwriting each other's snapshots."""
    light = _light()
    light.hass = SimpleNamespace()
    light._entry = SimpleNamespace(entry_id="e1")

    await light.async_added_to_hass()

    assert len(at_startup) == 1
    version, key = at_startup[0]
    assert version == 1
    assert "e1" in key
    assert light.unique_id in key


async def test_startup_takes_the_reboot_generation_as_its_baseline(at_startup):
    """Whatever generation exists when the entity starts is normal. Only a later
    poll reporting a newer one is a reboot that happened while it was alive, and
    starting from zero would make every restart look like one."""
    light = _light(coordinator=_Coordinator(generation=7))
    light.hass = SimpleNamespace()
    light._entry = SimpleNamespace(entry_id="e1")

    await light.async_added_to_hass()

    assert light._seen_reboot_generation == 7


async def test_a_recovery_that_throws_does_not_stop_the_entity(at_startup,
                                                               monkeypatch):
    """Being added to Home Assistant must not depend on the camera answering. An
    entity that failed to appear because a recovery threw would take the light
    away entirely, which is worse than not finishing the restore."""
    async def _explode(self):
        raise RuntimeError("the camera is not answering")

    monkeypatch.setattr(DahuaIlluminator, "_recover_persisted_override", _explode)
    light = _light(coordinator=_Coordinator(generation=4))
    light.hass = SimpleNamespace()
    light._entry = SimpleNamespace(entry_id="e1")

    await light.async_added_to_hass()

    assert light._seen_reboot_generation == 4, (
        "the baseline was not set, so the rest of startup was skipped")


async def test_a_recovery_that_throws_keeps_the_snapshot(at_startup, monkeypatch):
    """It may still be needed next restart. Deleting it because this attempt
    failed is the one thing that cannot be undone."""
    store = _Store(data=_snapshot())

    async def _explode(self):
        raise RuntimeError("the camera is not answering")

    monkeypatch.setattr(DahuaIlluminator, "_recover_persisted_override", _explode)
    light = _light(store)
    light.hass = SimpleNamespace()
    light._entry = SimpleNamespace(entry_id="e1")

    await light.async_added_to_hass()

    assert store.removed == 0


# --- putting the camera back with nothing saved -----------------------------

async def test_restoring_without_a_snapshot_still_releases_the_emitter():
    """`_restore_camera_lighting` is called with no saved light state when the
    entity never recorded one. It falls back to the coordinator's current channel
    and bank and forces the white light off, because leaving it on is the failure
    the user would actually notice."""
    coordinator = _Coordinator()
    light = _light(coordinator=coordinator)

    await light._restore_camera_lighting(None, None)

    assert coordinator.client.writes == [
        ("v2_raw", CHANNEL, "1", 0, "Off", "MiddleLight")]


async def test_restoring_with_no_scheme_stops_after_the_light():
    """A camera with no emitter-selection scheme has nothing else to put back, and
    writing a scheme it does not have is a request it will refuse."""
    coordinator = _Coordinator()
    light = _light(coordinator=coordinator)

    await light._restore_camera_lighting(
        None, (CHANNEL, "1", 0, "MiddleLight", "Off", None))

    assert [write[0] for write in coordinator.client.writes] == ["v2_raw"]
