"""Merging a recorder's per-channel entries into one, without losing anything.

A recorder was one config entry per channel, so removing a 64 channel NVR meant
64 deletions (#827). The migration moves each host onto one entry with a subentry
per channel.

It is the one thing in this integration that cannot be undone from inside Home
Assistant, so these tests are about what it must never do rather than only what
it should.

The order is the dangerous part and is pinned here by recording the sequence of
operations. A device belongs to exactly one config entry from Home Assistant
2026.8, so moving a device whose entities still point at the old entry makes Home
Assistant delete them. Measured: doing it that way round sent all 232 entities on
a real install to `deleted_entities`. These fakes do not model that cascade, on
purpose -- modelling it would only test the model. The real behaviour was proved
against a copy of a real registry; what is pinned here is that this code asks for
things in the order that keeps it safe.
"""

from types import SimpleNamespace

import pytest

from custom_components.dahua import migrate


class _Entry:
    def __init__(self, entry_id, address, channel, title=None):
        self.entry_id = entry_id
        self.data = {"address": address, "channel": channel}
        self.title = title or entry_id
        self.subentries = {}


class _Subentry:
    def __init__(self, data, subentry_type, title, unique_id):
        self.data = data
        self.subentry_type = subentry_type
        self.title = title
        self.unique_id = unique_id
        self.subentry_id = "sub-%s" % unique_id


class _ConfigEntries:
    def __init__(self, entries, log):
        self._entries = list(entries)
        self._log = log

    def async_entries(self, domain):
        return list(self._entries)

    def async_add_subentry(self, entry, subentry):
        # Home Assistant refuses a second subentry with a unique id the
        # entry already has (_raise_if_subentry_unique_id_exists). Accepting
        # it quietly here made a merge that could raise from inside itself,
        # halfway through moving entities, look safe.
        if any(s.unique_id == subentry.unique_id
               for s in entry.subentries.values()):
            raise ValueError("duplicate subentry unique id %s"
                             % subentry.unique_id)
        self._log.append(("subentry", entry.entry_id, subentry.unique_id))
        entry.subentries[subentry.subentry_id] = subentry
        return True

    async def async_remove(self, entry_id):
        self._log.append(("remove", entry_id))
        self._entries = [e for e in self._entries if e.entry_id != entry_id]


class _Registry:
    """Stands in for both registries; they are asked the same two questions."""

    def __init__(self, owned, log, kind):
        # entry_id -> list of row objects
        self._owned = {k: list(v) for k, v in owned.items()}
        self._log = log
        self._kind = kind

    def rows_for(self, entry_id):
        return list(self._owned.get(entry_id, []))

    def _move(self, row, new_entry):
        for entry_id, rows in self._owned.items():
            if row in rows:
                rows.remove(row)
                break
        self._owned.setdefault(new_entry, []).append(row)


class _Entities(_Registry):
    def async_update_entity_platform(self, entity_id, platform, *,
                                     new_config_entry_id, new_config_subentry_id):
        self._log.append(("entity", entity_id, new_config_entry_id))
        row = next(r for rows in self._owned.values() for r in rows
                   if r.entity_id == entity_id)
        self._move(row, new_config_entry_id)


class _Devices(_Registry):
    def async_update_device(self, device_id, *, new_config_entry_id,
                            new_config_subentry_id):
        self._log.append(("device", device_id, new_config_entry_id))
        row = next(r for rows in self._owned.values() for r in rows
                   if r.id == device_id)
        self._move(row, new_config_entry_id)


@pytest.fixture
def world(monkeypatch, tmp_path):
    """Two channels of one recorder, plus an unrelated single camera."""
    log = []
    entries = [
        _Entry("e0", "192.168.0.213", 0, "Channel 0"),
        _Entry("e1", "192.168.0.213", "1", "Channel 1"),   # a string, as stored
        _Entry("solo", "192.168.0.99", 0, "A camera"),
    ]
    entities = _Entities({
        "e0": [SimpleNamespace(entity_id="sensor.a")],
        "e1": [SimpleNamespace(entity_id="sensor.b"),
               SimpleNamespace(entity_id="sensor.c")],
        "solo": [SimpleNamespace(entity_id="sensor.solo")],
    }, log, "entity")
    devices = _Devices({
        "e0": [SimpleNamespace(id="d0")],
        "e1": [SimpleNamespace(id="d1")],
        "solo": [SimpleNamespace(id="dsolo")],
    }, log, "device")

    hass = SimpleNamespace(
        config_entries=_ConfigEntries(entries, log),
        config=SimpleNamespace(path=lambda *p: str(tmp_path.joinpath(*p))),
    )

    async def executor(func, *args):
        return func(*args)

    hass.async_add_executor_job = executor

    monkeypatch.setattr(migrate.er, "async_get", lambda _h: entities)
    monkeypatch.setattr(migrate.dr, "async_get", lambda _h: devices)
    monkeypatch.setattr(
        migrate.er, "async_entries_for_config_entry",
        lambda reg, entry_id: reg.rows_for(entry_id))
    monkeypatch.setattr(
        migrate.dr, "async_entries_for_config_entry",
        lambda reg, entry_id: reg.rows_for(entry_id))
    monkeypatch.setattr(migrate, "ConfigSubentry", _Subentry)
    monkeypatch.setattr(migrate, "_backup", lambda _h: str(tmp_path / "backup"))

    return SimpleNamespace(hass=hass, log=log, entries=entries,
                           entities=entities, devices=devices, tmp=tmp_path)


# --- the order, which is the part that destroys data when wrong -------------

async def test_every_entity_moves_before_any_device_does(world):
    await migrate.async_merge_channel_entries(world.hass)

    kinds = [step[0] for step in world.log]
    last_entity = max(i for i, kind in enumerate(kinds) if kind == "entity")
    first_device = min(i for i, kind in enumerate(kinds) if kind == "device")

    assert last_entity < first_device, (
        "a device moved before the entities were off the old entry, which is "
        "what makes Home Assistant delete them: %r" % (world.log,))


async def test_nothing_is_removed_until_everything_has_moved(world):
    await migrate.async_merge_channel_entries(world.hass)

    kinds = [step[0] for step in world.log]
    first_remove = min(i for i, kind in enumerate(kinds) if kind == "remove")

    assert max(i for i, kind in enumerate(kinds) if kind in ("entity", "device")) \
        < first_remove


# --- what it produces --------------------------------------------------------

async def test_one_entry_is_left_with_a_subentry_for_each_channel(world):
    await migrate.async_merge_channel_entries(world.hass)

    remaining = world.hass.config_entries.async_entries("dahua")
    survivor = [e for e in remaining if e.data["address"] == "192.168.0.213"]

    assert len(survivor) == 1
    assert sorted(s.unique_id for s in survivor[0].subentries.values()) == [
        "192.168.0.213_0", "192.168.0.213_1"]


def test_the_channel_is_read_as_a_number_however_it_was_stored():
    """Extra channels are stored as strings by the add flow, the first is absent
    entirely. A subentry keyed on the raw value would make `1` and `"1"` two
    different channels."""
    assert migrate._channel_of(_Entry("x", "a", "1")) == 1
    assert migrate._channel_of(_Entry("x", "a", 1)) == 1
    assert migrate._channel_of(SimpleNamespace(data={})) == 0
    assert migrate._channel_of(SimpleNamespace(data={"channel": None})) == 0


async def test_every_entity_ends_up_on_the_surviving_entry(world):
    await migrate.async_merge_channel_entries(world.hass)

    assert sorted(r.entity_id for r in world.entities.rows_for("e0")) == [
        "sensor.a", "sensor.b", "sensor.c"]


# --- a host that needs nothing doing ----------------------------------------

async def test_two_entries_on_one_channel_fold_into_one_subentry(world):
    """A device that reports no serial number got no unique id, so nothing
    stopped the same channel being added twice. Home Assistant raises on the
    second subentry with the same unique id, and that raise would have come
    from the middle of the merge -- after entities had moved, with the
    backup the only way back. Two entries for one channel are one channel.
    """
    world.entries.append(_Entry("dup", "192.168.0.213", 1, "Channel 1 again"))
    world.entities._owned["dup"] = [SimpleNamespace(entity_id="sensor.d")]
    world.devices._owned["dup"] = [SimpleNamespace(id="ddup")]

    await migrate.async_merge_channel_entries(world.hass)

    survivor = world.entries[0]
    assert [s.unique_id for s in survivor.subentries.values()] == [
        "192.168.0.213_0", "192.168.0.213_1"]
    # And the duplicate's entities came across rather than being stranded
    # on an entry the merge then refused to remove.
    moved = {e[1] for e in world.log if e[0] == "entity"}
    assert moved == {"sensor.a", "sensor.b", "sensor.c", "sensor.d"}
    assert ("remove", "dup") in world.log


async def test_a_single_camera_is_left_completely_alone(world):
    await migrate.async_merge_channel_entries(world.hass)

    assert [r.entity_id for r in world.entities.rows_for("solo")] == ["sensor.solo"]
    assert not any(step[1] == "solo" for step in world.log if len(step) > 1)
    assert [e.entry_id for e in world.hass.config_entries.async_entries("dahua")
            if e.data["address"] == "192.168.0.99"] == ["solo"]


async def test_a_second_run_changes_nothing(world):
    await migrate.async_merge_channel_entries(world.hass)
    settled = len(world.log)

    await migrate.async_merge_channel_entries(world.hass)

    assert len(world.log) == settled, "re-running moved something: %r" % (
        world.log[settled:],)


# --- the guard, which is the reason this is safe to ship --------------------

async def test_a_failed_entity_move_stops_before_any_device_is_touched(world,
                                                                       monkeypatch):
    """The check that prevents rather than reports.

    Moving a device is what makes Home Assistant delete entities that are still
    on the old entry, so a check afterwards can only count the damage. Stopping
    here costs nothing: no device has moved and no entry has been removed.
    """
    def refuse(*_args, **_kwargs):
        return None

    monkeypatch.setattr(world.entities, "async_update_entity_platform", refuse)

    await migrate.async_merge_channel_entries(world.hass)

    assert not [step for step in world.log if step[0] == "device"]
    assert not [step for step in world.log if step[0] == "remove"]
    assert len(world.hass.config_entries.async_entries("dahua")) == 3


async def test_an_entry_that_still_owns_entities_is_never_removed(world,
                                                                  monkeypatch):
    """Belt and braces for the entry removal itself: removing an entry takes its
    entities and their history with it, so it happens only when there are none."""
    real = world.entities.async_update_entity_platform

    def move_all_but_one(entity_id, platform, **kwargs):
        if entity_id == "sensor.c":
            return None
        return real(entity_id, platform, **kwargs)

    monkeypatch.setattr(world.entities, "async_update_entity_platform",
                        move_all_but_one)

    await migrate.async_merge_channel_entries(world.hass)

    assert not [step for step in world.log if step[0] == "remove"]
    assert [r.entity_id for r in world.entities.rows_for("e1")] == ["sensor.c"]


# --- and the backup, which is the only way back -----------------------------

async def test_no_host_is_touched_if_the_backup_cannot_be_written(world,
                                                                  monkeypatch):
    """The migration cannot be undone, so it does not start without a copy."""
    monkeypatch.setattr(migrate, "_backup", lambda _h: None)

    await migrate.async_merge_channel_entries(world.hass)

    assert world.log == []
