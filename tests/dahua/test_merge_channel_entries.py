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
    def __init__(self, entry_id, address, channel, title=None, port=None,
                 data=None, options=None, disabled_by=None):
        self.entry_id = entry_id
        self.data = {"address": address, "channel": channel}
        self.data.update(data or {})
        if port is not None:
            self.data["port"] = port
        # A real ConfigEntry always has both. This fake had only `data`, which is
        # exactly why nothing here noticed that the migration read only `data`:
        # the thing it dropped was not modelled.
        self.options = dict(options or {})
        # Also always present on a real ConfigEntry, and also not modelled here
        # before: a disabled entry was merged like any other.
        self.disabled_by = disabled_by
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
        # A real RegistryEntry carries config_subentry_id, and the #972 guard reads
        # it to tell a finished recorder from a merge still in progress. Record it
        # so a re-run sees the same shape Home Assistant would.
        row.config_subentry_id = new_config_subentry_id


class _Devices(_Registry):
    def async_update_device(self, device_id, *, new_config_entry_id,
                            new_config_subentry_id):
        self._log.append(("device", device_id, new_config_entry_id))
        row = next(r for rows in self._owned.values() for r in rows
                   if r.id == device_id)
        self._move(row, new_config_entry_id)
        row.config_subentry_id = new_config_subentry_id


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


async def test_a_card_raised_for_a_mixed_case_hostname_is_withdrawn(world):
    """The removal hook keys its card by normalize_address, which keeps the
    case, while the merge groups by a lowercased address. Deleting the
    lowercased id left a host configured as NVR.local with its card standing,
    still offering to delete the recorder the merge had just created."""
    for entry in world.entries:
        if entry.data["address"] == "192.168.0.213":
            entry.data["address"] = "NVR.local"

    await migrate.async_merge_channel_entries(world.hass)

    assert world.deleted_issues == ["siblings_remain_NVR.local"]


# --- what each channel is configured with, not what it was added with -------
#
# A subentry's data is where a merged recorder keeps one channel's own answers,
# and `channel_option` reads it *before* the entry's options. Copying only
# `entry.data` therefore did more than fail to carry a later change: it made the
# add-time value shadow it permanently, for every key stored in both. A channel
# switched from VideoMotion to SmartMotionHuman in Configure went back to
# VideoMotion at migration, and the entry's options could no longer correct it.
#
# Reported by alpha520098 on #825, who identified the key set as well.

def _recorder(monkeypatch, world, *, e0_options=None, e1_options=None,
              e0_data=None, e1_data=None):
    """Rebuild the two channels of the recorder with data and options of my own."""
    world.entries[0] = _Entry("e0", "192.168.0.213", 0, "Channel 0",
                              data=e0_data, options=e0_options)
    world.entries[1] = _Entry("e1", "192.168.0.213", "1", "Channel 1",
                              data=e1_data, options=e1_options)
    world.hass.config_entries._entries = list(world.entries)
    return world


def _subentries(world):
    """channel -> the subentry data the merge built for it."""
    survivor = world.hass.config_entries._entries[0]
    return {str(s.data.get("channel")): s.data
            for s in survivor.subentries.values()}


async def test_a_changed_event_selection_survives_the_merge(monkeypatch, world):
    """The reported case. The user picked SmartMotionHuman in Configure, which is
    stored in options; the list in data is whatever the camera was added with.
    """
    _recorder(monkeypatch, world,
              e1_data={"events": ["VideoMotion"]},
              e1_options={"events": ["SmartMotionHuman"]})

    await migrate.async_merge_channel_entries(world.hass)

    assert _subentries(world)["1"]["events"] == ["SmartMotionHuman"], (
        "the channel went back to the events it was added with")


async def test_an_empty_event_selection_survives_the_merge(monkeypatch, world):
    """Choosing nothing is a choice, and it is the one a falsy check loses. The
    channel wanted no events and would have been given nine back.
    """
    _recorder(monkeypatch, world,
              e1_data={"events": ["VideoMotion", "AlarmLocal"]},
              e1_options={"events": []})

    await migrate.async_merge_channel_entries(world.hass)

    assert _subentries(world)["1"]["events"] == []


async def test_the_surviving_channel_keeps_its_own_settings_too(monkeypatch, world):
    """The survivor gets a subentry like every other channel, so its own options
    have to be carried into it. Before this, its subentry shadowed them with its
    add-time data exactly as the others did, which is easy to miss because the
    survivor is also the entry whose options are still there.
    """
    _recorder(monkeypatch, world,
              e0_data={"events": ["VideoMotion"]},
              e0_options={"events": ["FaceDetection"]})

    await migrate.async_merge_channel_entries(world.hass)

    assert _subentries(world)["0"]["events"] == ["FaceDetection"]


async def test_a_per_channel_toggle_survives(monkeypatch, world):
    """Not only events. `manual_siren` exists because one camera on a recorder has
    the hardware and the others do not, so losing it is losing the siren.
    """
    _recorder(monkeypatch, world,
              e1_options={"manual_siren": True, "authorized_plates": "AB12CDE"})

    await migrate.async_merge_channel_entries(world.hass)

    carried = _subentries(world)["1"]
    assert carried["manual_siren"] is True
    assert carried["authorized_plates"] == "AB12CDE"


async def test_a_host_wide_option_is_not_frozen_onto_a_channel(monkeypatch, world):
    """The other half of the fix. Copying every option would put host settings in
    each subentry, where `channel_option` reads them before the entry's, so
    changing the host's poll interval afterwards would leave every channel on the
    value it had at migration.
    """
    _recorder(monkeypatch, world,
              e1_options={"scan_interval": 45, "events": ["VideoMotion"]})

    await migrate.async_merge_channel_entries(world.hass)

    carried = _subentries(world)["1"]
    assert "scan_interval" not in carried, (
        "a host-wide option was pinned onto the channel")
    assert carried["events"] == ["VideoMotion"]


async def test_an_entry_with_no_options_is_unchanged(monkeypatch, world):
    """The common case, and the one that must not be disturbed: a recorder nobody
    has reconfigured migrates exactly as it did before.
    """
    _recorder(monkeypatch, world,
              e1_data={"events": ["VideoMotion"], "name": "Channel 1"})

    await migrate.async_merge_channel_entries(world.hass)

    carried = _subentries(world)["1"]
    assert carried["events"] == ["VideoMotion"]
    assert carried["name"] == "Channel 1"


# --- an entry somebody switched off stays switched off ----------------------
#
# Disabling an entry is how a channel whose camera has gone gets parked without
# throwing its history away. Nothing in the merge looked at `disabled_by`, and
# `async_entries` returns disabled entries like any other, so both of these
# followed:
#
#   * a disabled channel was folded into the enabled survivor and started
#     polling again, which is the opposite of what the user asked for;
#   * the survivor is `ordered[0]`, the lowest channel, so a disabled channel 0
#     *became* the survivor. Every other channel's entities move onto an entry
#     Home Assistant never sets up, and the entries they came from are then
#     removed. The whole recorder goes dark on upgrade.
#
# Home Assistant does not stop either: async_update_entity_platform checks that
# the entity is unloaded and that a config entry was named, and nothing about
# whether the entry it is moving to is disabled.

async def test_a_disabled_channel_is_left_alone(world):
    """A host that really does merge, with one disabled channel among the others.

    It keeps its own entry, its entities and its history. Folding it in would move
    its entities onto the enabled survivor, so a camera the user switched off would
    start polling again with nothing said about it.
    """
    world.entries[1] = _Entry("e1", "192.168.0.213", "1", "Channel 1",
                              disabled_by="user")
    world.entries.append(_Entry("e2", "192.168.0.213", "2", "Channel 2"))
    world.entities._owned["e2"] = [SimpleNamespace(entity_id="sensor.d")]
    world.devices._owned["e2"] = [SimpleNamespace(id="d2")]
    world.hass.config_entries._entries = list(world.entries)

    await migrate.async_merge_channel_entries(world.hass)

    # The merge did happen, so this is not passing on an empty run.
    assert ("remove", "e2") in world.log, world.log
    assert ("remove", "e1") not in world.log, "a disabled entry was removed"
    moved = [step for step in world.log
             if step[0] == "entity" and step[1] in ("sensor.b", "sensor.c")]
    assert not moved, (
        "a disabled channel's entities were moved off its entry: %s" % moved)


async def test_a_disabled_lowest_channel_never_becomes_the_survivor(world):
    """The one that takes a recorder down. Channel 0 disabled, channels 1 and 2
    enabled: nothing may be moved onto e0, because Home Assistant does not set up
    a disabled entry and the entries the entities came from are removed afterwards.
    """
    world.entries[0] = _Entry("e0", "192.168.0.213", 0, "Channel 0",
                              disabled_by="user")
    world.entries.append(_Entry("e2", "192.168.0.213", "2", "Channel 2"))
    world.entities._owned["e2"] = [SimpleNamespace(entity_id="sensor.d")]
    world.devices._owned["e2"] = [SimpleNamespace(id="d2")]
    world.hass.config_entries._entries = list(world.entries)

    await migrate.async_merge_channel_entries(world.hass)

    onto_disabled = [step for step in world.log
                     if step[0] in ("entity", "device") and step[2] == "e0"]
    assert not onto_disabled, (
        "entities were moved onto a disabled entry, which Home Assistant never "
        "sets up: %s" % onto_disabled)
    # And the merge still happened, onto an entry that is actually enabled.
    survivor = next(e for e in world.hass.config_entries._entries
                    if e.entry_id == "e1")
    assert survivor.disabled_by is None
    assert survivor.subentries, "nothing merged at all"


async def test_the_enabled_channels_still_merge_around_it(world):
    """The fix must not stop the merge happening. Channel 0 disabled, 1 and 2
    enabled, so those two become one entry and the disabled one stays beside them.
    """
    world.entries[0] = _Entry("e0", "192.168.0.213", 0, "Channel 0",
                              disabled_by="user")
    world.entries.append(_Entry("e2", "192.168.0.213", "2", "Channel 2"))
    world.entities._owned["e2"] = [SimpleNamespace(entity_id="sensor.d")]
    world.devices._owned["e2"] = [SimpleNamespace(id="d2")]
    world.hass.config_entries._entries = list(world.entries)

    await migrate.async_merge_channel_entries(world.hass)

    survivor = next(e for e in world.hass.config_entries._entries
                    if e.entry_id == "e1")
    assert survivor.subentries, "the enabled channels did not merge"
    assert ("remove", "e2") in world.log


async def test_a_host_whose_only_other_entry_is_disabled_does_nothing(world):
    """One enabled channel plus one disabled is not a recorder worth merging, and
    it must not write a backup or a warning on every startup for ever.
    """
    world.entries[1] = _Entry("e1", "192.168.0.213", "1", "Channel 1",
                              disabled_by="user")
    world.hass.config_entries._entries = list(world.entries)

    await migrate.async_merge_channel_entries(world.hass)

    assert world.log == [], world.log


async def test_a_host_with_every_entry_disabled_does_nothing(world):
    """Nothing to merge and nothing that could be set up afterwards."""
    for index, entry in enumerate(world.entries):
        if entry.entry_id in ("e0", "e1"):
            world.entries[index] = _Entry(
                entry.entry_id, "192.168.0.213", entry.data["channel"],
                entry.title, disabled_by="user")
    world.hass.config_entries._entries = list(world.entries)

    await migrate.async_merge_channel_entries(world.hass)

    assert world.log == [], world.log


# --- #972: a host that already has a finished recorder must not be re-merged ----
#
# Re-merging reads a finished recorder as a single channel and moves every channel
# it owns onto one subentry, discarding the rest. No entity is lost, so the count
# guards do not catch it; the per-channel structure is destroyed. It arises when a
# channel disabled at the first merge is re-enabled, leaving a finished recorder
# beside a flat entry. These pin that the host is left untouched, while a merge
# still in progress (subentries created, entities not yet spread) still completes.

A = "192.168.0.213"


def _sub(channel):
    return _Subentry({"address": A, "channel": str(channel)}, migrate.CHANNEL_SUBENTRY,
                     "Channel %s" % channel, "%s_%s" % (A, channel))


def _row(entity_id, subentry):
    return SimpleNamespace(entity_id=entity_id, config_subentry_id=subentry)


async def test_a_finished_recorder_is_not_refolded_by_a_lower_re_enabled_channel(world):
    """Flat channel 0 re-enabled beside a finished recorder (channels 1 and 2)."""
    merged = world.entries[1]
    merged.data["channel"] = "1"
    s1, s2 = _sub(1), _sub(2)
    merged.subentries = {s1.subentry_id: s1, s2.subentry_id: s2}
    world.entities._owned["e1"] = [
        _row("sensor.ch1", s1.subentry_id),
        _row("sensor.ch2", s2.subentry_id),
    ]

    await migrate.async_merge_channel_entries(world.hass)

    assert world.log == [], world.log
    assert {r.config_subentry_id for r in world.entities.rows_for("e1")} == {
        s1.subentry_id, s2.subentry_id}


async def test_a_finished_recorder_is_not_refolded_when_it_is_the_survivor(world):
    """Higher channel re-enabled: the finished recorder is channel 0 and would be
    the survivor, so its own channels would collapse. The commoner trigger."""
    merged = world.entries[0]
    s0, s1, s2 = _sub(0), _sub(1), _sub(2)
    merged.subentries = {s.subentry_id: s for s in (s0, s1, s2)}
    world.entities._owned["e0"] = [
        _row("sensor.ch0", s0.subentry_id),
        _row("sensor.ch1", s1.subentry_id),
        _row("sensor.ch2", s2.subentry_id),
    ]
    world.entries[1].data["channel"] = "5"  # the flat, re-enabled channel
    world.entities._owned["e1"] = [_row("sensor.ch5", None)]

    await migrate.async_merge_channel_entries(world.hass)

    assert world.log == [], world.log
    assert len({r.config_subentry_id for r in world.entities.rows_for("e0")}) == 3


async def test_a_device_only_channel_still_marks_a_finished_recorder(world):
    """A channel that registered a device but no entity: entity spread is one, so
    the device spread is what has to catch it."""
    merged = world.entries[0]
    s0, s1 = _sub(0), _sub(1)
    merged.subentries = {s0.subentry_id: s0, s1.subentry_id: s1}
    world.entities._owned["e0"] = [_row("sensor.ch0", s0.subentry_id)]  # ch1: no entity
    world.devices._owned["e0"] = [SimpleNamespace(id="d0", config_subentry_id=s0.subentry_id),
                                  SimpleNamespace(id="d1", config_subentry_id=s1.subentry_id)]
    world.entries[1].data["channel"] = "5"
    world.entities._owned["e1"] = [_row("sensor.ch5", None)]

    await migrate.async_merge_channel_entries(world.hass)

    assert world.log == [], world.log


async def test_a_merge_still_in_progress_is_re_entered_and_finished(world):
    """The recovery path the guard must not break: subentries created, but the
    survivor's entities are not yet spread (its own channel only), and the flat
    leftovers still own theirs. This must complete, not be skipped."""
    survivor = world.entries[0]
    s0, s1, s2 = _sub(0), _sub(1), _sub(2)
    survivor.subentries = {s.subentry_id: s for s in (s0, s1, s2)}
    world.entities._owned["e0"] = [_row("sensor.ch0", s0.subentry_id)]  # spread == 1
    # e1 (channel 1) still owns its entity, flat; add e2 (channel 2) likewise.
    world.entities._owned["e1"] = [_row("sensor.ch1", None)]
    e2 = _Entry("e2", A, "2", "Channel 2")
    world.entries.append(e2)
    world.hass.config_entries._entries.append(e2)
    world.entities._owned["e2"] = [_row("sensor.ch2", None)]
    world.devices._owned["e2"] = [SimpleNamespace(id="d2")]

    await migrate.async_merge_channel_entries(world.hass)

    assert ("remove", "e1") in world.log and ("remove", "e2") in world.log, world.log
    landed = {r.entity_id: r.config_subentry_id
              for r in world.entities.rows_for("e0")}
    assert len(set(landed.values())) == 3, landed  # ch0, ch1, ch2 on distinct subentries
