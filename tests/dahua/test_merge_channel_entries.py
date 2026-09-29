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
    def __init__(self, entry_id, address, channel, title=None, port=None):
        self.entry_id = entry_id
        self.data = {"address": address, "channel": channel}
        if port is not None:
            self.data["port"] = port
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

    deleted_issues = []
    monkeypatch.setattr(
        migrate.ir, "async_delete_issue",
        lambda _hass, _domain, issue_id: deleted_issues.append(issue_id))

    return SimpleNamespace(hass=hass, log=log, entries=entries,
                           entities=entities, devices=devices, tmp=tmp_path,
                           deleted_issues=deleted_issues)


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
    dup = _Entry("dup", "192.168.0.213", 1, "Channel 1 again")
    world.entries.append(dup)
    # _ConfigEntries took a copy of the list when the fixture built it, so an
    # entry added only to world.entries is one the merge never sees.
    world.hass.config_entries._entries.append(dup)
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


# --- the second count check, and the removal guard --------------------------
#
# There are two count checks, and the tests above only reach the first.
#
#   line 230  before any device is touched -- the one that PREVENTS the loss
#   line 255  after the device moves -- belt and braces, which can only REPORT it
#
# A failed entity move trips the first and returns, so everything after it was
# unreached: the second check, and the per-entry guard on removal. Those are the last
# things standing between a partly-done merge and a removal that takes entities with it,
# and what they exist for is documented in the source as measured fact -- 232 entities
# sent to deleted_entities on a real install.
#
# The fakes deliberately do not model Home Assistant's delete-on-move cascade, because
# modelling it would only test the model. These two simulate the *result* of it, which is
# the only way to reach code whose precondition the earlier check normally prevents.


async def test_an_entity_lost_while_devices_move_stops_the_removal(world,
                                                                   monkeypatch):
    """The second check. Every entity moved, so the first check passed and the device
    moves went ahead; then an entity is gone. Nothing may be removed after that, because
    the old entries are the only place those entities can still be."""
    real = world.devices.async_update_device

    def lose_an_entity(device_id, **kwargs):
        result = real(device_id, **kwargs)
        rows = world.entities._owned.get("e0")
        if rows:
            rows.pop()          # what the cascade looks like from outside
        return result

    monkeypatch.setattr(world.devices, "async_update_device", lose_an_entity)

    await migrate.async_merge_channel_entries(world.hass)

    assert not [step for step in world.log if step[0] == "remove"], (
        "removed an entry after losing an entity, so the entities have nowhere left")
    assert any(step[0] == "device" for step in world.log), (
        "the device moves never happened, so this reached the earlier check instead")


async def test_an_entry_still_owning_something_at_removal_is_kept(world,
                                                                  monkeypatch):
    """The per-entry guard, which the first check normally makes unreachable. Both counts
    add up here and one old entry still owns a row, so that entry is kept and the others
    are still removed: an incomplete merge is recoverable, a removed entry is not."""
    real = world.devices.async_update_device
    added = []

    def strand_a_row(device_id, **kwargs):
        result = real(device_id, **kwargs)
        if not added:
            added.append(1)
            world.entities._owned.setdefault("e1", []).append(
                SimpleNamespace(entity_id="sensor.stranded"))
        return result

    monkeypatch.setattr(world.devices, "async_update_device", strand_a_row)

    await migrate.async_merge_channel_entries(world.hass)

    removed = [step[1] for step in world.log if step[0] == "remove"]
    assert "e1" not in removed, "removed an entry that still owned an entity"
    assert [r.entity_id for r in world.entities.rows_for("e1")] == ["sensor.stranded"]


# --- the backup itself ------------------------------------------------------

def test_the_backup_copies_the_registries_it_finds(tmp_path):
    """Named files only, and only the ones that exist: a fresh install has no
    core.restore_state, and a missing file is not a reason to refuse to migrate."""
    import os

    storage = tmp_path / ".storage"
    storage.mkdir()
    (storage / migrate.BACKED_UP[0]).write_text("first")
    hass = SimpleNamespace(
        config=SimpleNamespace(path=lambda *p: str(tmp_path.joinpath(*p))))

    target = migrate._backup(hass)

    assert target is not None
    assert os.path.exists(os.path.join(target, migrate.BACKED_UP[0]))


def test_a_backup_that_copied_nothing_is_no_backup(tmp_path):
    """An empty directory is not a way back, and the caller treats None as "do not
    migrate"."""
    (tmp_path / ".storage").mkdir()
    hass = SimpleNamespace(
        config=SimpleNamespace(path=lambda *p: str(tmp_path.joinpath(*p))))

    assert migrate._backup(hass) is None


def test_a_backup_that_cannot_be_written_is_no_backup(tmp_path, monkeypatch):
    """A full or read-only disk must read as "no backup" rather than as an exception out
    of setup, and the caller then leaves every entry alone."""
    import shutil

    storage = tmp_path / ".storage"
    storage.mkdir()
    (storage / migrate.BACKED_UP[0]).write_text("first")
    monkeypatch.setattr(shutil, "copy2",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    hass = SimpleNamespace(
        config=SimpleNamespace(path=lambda *p: str(tmp_path.joinpath(*p))))

    assert migrate._backup(hass) is None


# --- reading the channel out of an entry ------------------------------------

def test_a_channel_stored_as_a_string_is_a_number():
    """The add flow writes it as a string for extra channels and leaves it absent for the
    first, so both shapes are in the wild."""
    assert migrate._channel_of(_Entry("e", "10.0.0.1", "3")) == 3
    assert migrate._channel_of(_Entry("e", "10.0.0.1", 3)) == 3


def test_a_channel_that_is_not_a_number_reads_as_zero():
    """A hand-edited .storage can hold anything, and raising here would take setup down
    before the migration could decide not to run."""
    assert migrate._channel_of(_Entry("e", "10.0.0.1", "not a number")) == 0
    assert migrate._channel_of(_Entry("e", "10.0.0.1", None)) == 0


# --- the device, not just the address ---------------------------------------
#
# Two Dahua boxes can sit behind one IP on different ports, which the rest of the
# integration already treats as two devices (client.py's device_key). Grouping by
# address alone folded the second device's channel 0 onto the first device's
# subentry: its configuration was dropped and its entities were re-parented to
# the wrong camera.

async def test_two_devices_behind_one_address_are_not_merged(world):
    other = _Entry("e81", "192.168.0.213", 0, "The camera on 81", port="81")
    world.entries.append(other)
    # _ConfigEntries took a copy of the list when the fixture built it, so an
    # entry added only to world.entries is one the merge never sees.
    world.hass.config_entries._entries.append(other)
    world.entities._owned["e81"] = [SimpleNamespace(entity_id="sensor.p81")]
    world.devices._owned["e81"] = [SimpleNamespace(id="d81")]

    await migrate.async_merge_channel_entries(world.hass)

    assert other.subentries == {}, "a second device on another port was folded in"
    assert [r.entity_id for r in world.entities.rows_for("e81")] == ["sensor.p81"]
    assert not [step for step in world.log
                if step[0] == "remove" and step[1] == "e81"]


async def test_an_entry_with_the_default_port_joins_one_that_states_it(world):
    """An entry from before the field existed was using port 80, so it is the
    same device as one that says 80."""
    world.entries[1].data["port"] = "80"

    await migrate.async_merge_channel_entries(world.hass)

    survivor = [e for e in world.entries
                if e.data["address"] == "192.168.0.213"][0]
    assert sorted(s.unique_id for s in survivor.subentries.values()) == [
        "192.168.0.213_0", "192.168.0.213_1"]


# --- the card the merge must not leave behind -------------------------------

async def test_the_siblings_card_the_removals_raised_is_withdrawn(world):
    """Each removal runs the manual-deletion hook, which sees the surviving entry
    as a sibling and raises a card whose fix removes every entry at the address --
    the recorder this merge has just finished creating. Nothing is left to offer,
    so the card it raised is withdrawn rather than left for somebody to click."""
    await migrate.async_merge_channel_entries(world.hass)

    assert world.deleted_issues == ["siblings_remain_192.168.0.213"]
