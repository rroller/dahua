"""An entry moves off the identity it derived from its credentials.

A device that will not state a serial gets `md5(address_rtspport_user_pass)`,
and every entity `unique_id`, every device identifier and the entry's own
unique id is built from it. That is #805: change the password and the same
hardware arrives as a new device with every entity rebuilt under a new id.

An indoor monitor always hashed, because it answers 404 to `magicBox.cgi` for
everything, while answering `magicBox.getSerialNo` over RPC2 perfectly well.
#1103 taught the client to ask but held the answer to monitors on the DHIP
transport, because a VTH that serves HTTP has entities in the registry today
and handing it the serial would rename all of them. @kmich reported that
against three VTH2421F-P whose ids had already moved once when he changed
their password, and made the case for doing it properly. This is that.

**Against a real registry, not a fake.** The one thing that can go wrong here
is Home Assistant deleting rows, and a fake registry cannot tell you that it
would: the #827 merge sent 232 entities to `deleted_entities` in testing
before the ordering was right. So these build real entity and device registry
rows and assert on what is left afterwards.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.dahua import migrate

HASH = "639d060c87813c613a7c217f6c4056bf"
SERIAL = "3E0164EPAN00095"


def _coordinator(hash_value=HASH, serial=SERIAL):
    return SimpleNamespace(
        client=SimpleNamespace(credential_hash=lambda: hash_value),
        get_device_serial_number=lambda: serial,
    )


async def _entry_with_rows(hass, unique_id=HASH, rows=("silence", "ring_volume")):
    """An entry filed under the generated identity, as an upgrade finds it."""
    entry = MockConfigEntry(
        domain="dahua",
        data={"address": "10.0.0.230"},
        unique_id=unique_id,
    )
    entry.add_to_hass(hass)

    devices = dr.async_get(hass)
    device = devices.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={("dahua", HASH)},
        name="Hallway Monitor",
    )
    entities = er.async_get(hass)
    created = [
        entities.async_get_or_create(
            "switch",
            "dahua",
            "{0}_{1}".format(HASH, suffix),
            config_entry=entry,
            device_id=device.id,
            suggested_object_id="hallway_monitor_{0}".format(suffix),
        )
        for suffix in rows
    ]
    return entry, device, created


REAL_BACKUP = migrate._backup


@pytest.fixture(autouse=True)
def backups(monkeypatch, tmp_path):
    """The real one copies .storage, which a test hass does not have on disk.

    Recorded rather than only stubbed, because "nothing was touched" is a
    claim about whether this even started, and a backup is the first thing it
    does. Several tests below assert on an empty list here.
    """
    taken = []

    def _backup(hass, label="x"):
        taken.append(label)
        return str(tmp_path)

    monkeypatch.setattr(migrate, "_backup", _backup)
    return taken


# --- the move itself --------------------------------------------------------


async def test_every_entity_keeps_its_entity_id_and_gains_the_real_serial(hass):
    """The whole point. A rename here would take the user's dashboards,
    automations, scripts, areas and recorder history with it, which is what
    letting Home Assistant rebuild would have done."""
    entry, _, created = await _entry_with_rows(hass)
    before = [row.entity_id for row in created]

    moved = await migrate.async_adopt_the_real_serial(hass, entry, [_coordinator()])

    entities = er.async_get(hass)
    assert moved == 2
    assert [entities.async_get(e).entity_id for e in before] == before
    assert sorted(entities.async_get(e).unique_id for e in before) == [
        "{0}_ring_volume".format(SERIAL),
        "{0}_silence".format(SERIAL),
    ]


async def test_nothing_is_deleted(hass):
    """The #827 failure mode, asserted directly rather than inferred.

    Worth being exact about what this does and does not establish. Swapping
    the two loops here does **not** make it fail, so it is not a proof that
    entities-before-devices is required: this migration rewrites identifiers
    in place rather than re-parenting between entries, which is the operation
    that lost 232 rows. It is a guard that no row goes to `deleted_entities`
    whatever else changes, which is the outcome anyone upgrading cares about.
    The ordering is kept because the operation is close enough that inverting
    it buys nothing.
    """
    entry, _, created = await _entry_with_rows(hass)

    await migrate.async_adopt_the_real_serial(hass, entry, [_coordinator()])

    entities = er.async_get(hass)
    assert entities.deleted_entities == {}
    assert len(er.async_entries_for_config_entry(entities, entry.entry_id)) == len(
        created
    )


async def test_the_device_is_rewritten_in_place_rather_than_replaced(hass):
    """A new device under the new identifiers would leave this one behind as
    an empty ghost, carrying the area, the name the user chose and the
    labels."""
    entry, device, _ = await _entry_with_rows(hass)

    await migrate.async_adopt_the_real_serial(hass, entry, [_coordinator()])

    after = dr.async_get(hass).async_get(device.id)
    assert after is not None
    assert after.identifiers == {("dahua", SERIAL)}
    assert after.name == "Hallway Monitor"


async def test_the_entrys_own_unique_id_moves(hass):
    """Or DHCP discovery and a re-add stop recognising the entry and offer to
    add the same monitor a second time."""
    entry, _, _ = await _entry_with_rows(hass)

    await migrate.async_adopt_the_real_serial(hass, entry, [_coordinator()])

    assert entry.unique_id == SERIAL


async def test_a_channel_suffix_survives_the_move(hass):
    """`get_serial_number` is the bare serial on channel 0 and `serial_N`
    above it, and every unique id is that plus a suffix. One prefix
    replacement has to carry both shapes."""
    entry = MockConfigEntry(
        domain="dahua", data={"address": "10.0.0.5"}, unique_id=None
    )
    entry.add_to_hass(hass)
    entities = er.async_get(hass)
    row = entities.async_get_or_create(
        "sensor", "dahua", "{0}_2_firmware".format(HASH), config_entry=entry
    )

    await migrate.async_adopt_the_real_serial(hass, entry, [_coordinator()])

    assert entities.async_get(row.entity_id).unique_id == "{0}_2_firmware".format(
        SERIAL
    )


# --- and when it must do nothing --------------------------------------------


async def test_a_second_run_changes_nothing(hass, backups):
    """It runs on every setup of every entry, so it has to be a no-op once
    the rows are already right, and must not take a second backup either."""
    entry, _, created = await _entry_with_rows(hass)
    await migrate.async_adopt_the_real_serial(hass, entry, [_coordinator()])

    again = await migrate.async_adopt_the_real_serial(hass, entry, [_coordinator()])

    entities = er.async_get(hass)
    assert again == 0
    assert backups == ["pre-serial"]
    assert [entities.async_get(r.entity_id).unique_id for r in created] == [
        "{0}_silence".format(SERIAL),
        "{0}_ring_volume".format(SERIAL),
    ]


async def test_a_device_that_already_reports_a_serial_is_untouched(hass, backups):
    """The overwhelming majority. Nothing here is filed under a hash, so
    there is nothing to recognise and no backup is taken."""
    entry = MockConfigEntry(
        domain="dahua", data={"address": "10.0.0.5"}, unique_id="4L07AD6PAG5F0F2"
    )
    entry.add_to_hass(hass)
    entities = er.async_get(hass)
    row = entities.async_get_or_create(
        "sensor", "dahua", "4L07AD6PAG5F0F2_firmware", config_entry=entry
    )

    moved = await migrate.async_adopt_the_real_serial(hass, entry, [_coordinator()])

    assert moved == 0
    assert backups == []
    assert entities.async_get(row.entity_id).unique_id == "4L07AD6PAG5F0F2_firmware"


async def test_a_device_still_identified_by_its_credentials_is_untouched(hass, backups):
    """A camera whose CGI is absent and which is not a monitor keeps hashing,
    so the serial it reports *is* the hash and there is nothing to move.

    Asserted on the backup rather than on the rows, because rewriting a row
    to the value it already holds changes nothing visible: this would have
    passed either way and proved nothing. What the equality guard really
    prevents is the whole run, and copying three registry files on every
    setup of every unidentifiable camera.
    """
    entry, _, created = await _entry_with_rows(hass)

    moved = await migrate.async_adopt_the_real_serial(
        hass, entry, [_coordinator(serial=HASH)]
    )

    assert moved == 0
    assert backups == []
    assert er.async_get(hass).async_get(created[0].entity_id).unique_id.startswith(HASH)


async def test_another_entrys_rows_are_not_touched(hass):
    """Two monitors behind one NAT would share an address but not a serial,
    and the hash is per device. Only this entry's rows move."""
    entry, _, _ = await _entry_with_rows(hass)
    other = MockConfigEntry(domain="dahua", data={"address": "10.0.0.231"})
    other.add_to_hass(hass)
    entities = er.async_get(hass)
    stranger = entities.async_get_or_create(
        "switch", "dahua", "{0}_silence".format(HASH), config_entry=other
    )

    await migrate.async_adopt_the_real_serial(hass, entry, [_coordinator()])

    assert entities.async_get(stranger.entity_id).unique_id == "{0}_silence".format(
        HASH
    )


async def test_backup_failure_stops_before_registry_changes(hass, monkeypatch):
    entry, _, created = await _entry_with_rows(hass)
    monkeypatch.setattr(migrate, "_backup", lambda hass, label="x": None)
    with pytest.raises(ValueError, match="back"):
        await migrate.async_adopt_the_real_serial(hass, entry, [_coordinator()])
    assert er.async_get(hass).async_get(created[0].entity_id).unique_id.startswith(HASH)
    assert entry.unique_id == HASH


async def test_entity_target_conflict_stops_the_entire_migration(hass):
    entry, device, created = await _entry_with_rows(hass)
    entities = er.async_get(hass)
    entities.async_get_or_create(
        "switch", "dahua", SERIAL + "_silence", config_entry=entry, device_id=device.id
    )
    with pytest.raises(ValueError, match="conflict"):
        await migrate.async_adopt_the_real_serial(hass, entry, [_coordinator()])
    assert [entities.async_get(r.entity_id).unique_id for r in created] == [
        HASH + "_silence",
        HASH + "_ring_volume",
    ]
    assert dr.async_get(hass).async_get(device.id).identifiers == {("dahua", HASH)}
    assert entry.unique_id == HASH


async def test_device_target_conflict_stops_before_any_entity_moves(hass):
    entry, _, created = await _entry_with_rows(hass)
    other = MockConfigEntry(domain="dahua", data={"address": "10.0.0.231"})
    other.add_to_hass(hass)
    dr.async_get(hass).async_get_or_create(
        config_entry_id=other.entry_id, identifiers={("dahua", SERIAL)}
    )
    with pytest.raises(ValueError, match="conflict"):
        await migrate.async_adopt_the_real_serial(hass, entry, [_coordinator()])
    assert (
        er.async_get(hass).async_get(created[0].entity_id).unique_id
        == HASH + "_silence"
    )
    assert entry.unique_id == HASH


async def test_config_entry_target_conflict_stops_before_any_entity_moves(hass):
    entry, _, created = await _entry_with_rows(hass)
    other = MockConfigEntry(domain="dahua", unique_id=SERIAL)
    other.add_to_hass(hass)
    with pytest.raises(ValueError, match="conflict"):
        await migrate.async_adopt_the_real_serial(hass, entry, [_coordinator()])
    assert (
        er.async_get(hass).async_get(created[0].entity_id).unique_id
        == HASH + "_silence"
    )
    assert entry.unique_id == HASH


async def test_password_change_does_not_hide_the_registrys_old_hash(hass):
    entry, _, created = await _entry_with_rows(hass)
    moved = await migrate.async_adopt_the_real_serial(
        hass, entry, [_coordinator(hash_value="a" * 32)]
    )
    assert moved == 2
    assert (
        er.async_get(hass).async_get(created[0].entity_id).unique_id
        == SERIAL + "_silence"
    )
    assert entry.unique_id == SERIAL


async def test_hash_prefix_requires_a_boundary(hass, backups):
    entry = MockConfigEntry(domain="dahua", unique_id=HASH + "a")
    entry.add_to_hass(hass)
    entities = er.async_get(hass)
    row = entities.async_get_or_create(
        "switch", "dahua", HASH + "a_silence", config_entry=entry
    )
    moved = await migrate.async_adopt_the_real_serial(hass, entry, [_coordinator()])
    assert moved == 0
    assert backups == []
    assert entities.async_get(row.entity_id).unique_id == HASH + "a_silence"


def test_serial_backup_requires_all_three_files(tmp_path):
    (tmp_path / ".storage").mkdir()
    (tmp_path / ".storage" / "core.config_entries").write_text("credentials")
    hass = SimpleNamespace(
        config=SimpleNamespace(path=lambda *p: str(tmp_path.joinpath(*p)))
    )
    assert REAL_BACKUP(hass, "pre-serial") is None


def test_serial_backups_are_private_and_cannot_overwrite_each_other(tmp_path):
    import os

    storage = tmp_path / ".storage"
    storage.mkdir()
    for name in migrate.BACKED_UP:
        (storage / name).write_text("before")
    hass = SimpleNamespace(
        config=SimpleNamespace(path=lambda *p: str(tmp_path.joinpath(*p)))
    )
    first = REAL_BACKUP(hass, "pre-serial")
    (storage / "core.config_entries").write_text("after")
    second = REAL_BACKUP(hass, "pre-serial")
    assert first != second
    assert (Path(first) / "core.config_entries").read_text() == "before"
    assert os.stat(first).st_mode & 0o077 == 0


async def test_a_target_claimed_during_backup_stops_before_changes(hass, monkeypatch):
    entry, device, created = await _entry_with_rows(hass)
    entities = er.async_get(hass)

    async def backup_job(*args):
        entities.async_get_or_create(
            "switch",
            "dahua",
            SERIAL + "_silence",
            config_entry=entry,
            device_id=device.id,
        )
        return "backup"

    monkeypatch.setattr(hass, "async_add_executor_job", backup_job)
    with pytest.raises(ValueError, match="conflict"):
        await migrate.async_adopt_the_real_serial(hass, entry, [_coordinator()])
    assert entities.async_get(created[0].entity_id).unique_id == HASH + "_silence"
    assert entry.unique_id == HASH


# --- a refusal has to name both sides (#1143) -------------------------------
#
# @wiiNinja upgraded 1.3.1 to 2.0 and setup stopped with
#
#     Serial migration entity conflict: binary_sensor.amcrestnvr_motion_alarm
#
# and nothing else. That is one entity ID out of two, and the one it names is
# the row being MOVED rather than whatever is standing in its place, so the
# only action it supports is deleting the wrong row. He deleted the wrong row.
#
# Stopping is right, and the tests above hold that: a half-finished move is
# far worse than a failed one. What was wrong is that the entry then retried
# for ever on a condition no amount of retrying clears, with nothing to act on.
#
# Two different collisions produce that one sentence and they want opposite
# fixes, so it has to say which:
#
#   - two legacy rows both want one new identity, which is an #805 duplicate
#     set after an address or password change
#   - the new identity is already held, so the legacy row has nowhere to go
#
# ⚠ And none of it may print a unique ID. The identity being left behind is
# md5(address_rtspport_username_password); this text lands on the UI and gets
# screenshotted into issues, and that is the password offered up for an
# offline attack. The parametrised test at the end is the one that holds that
# line, and it covers every refusal rather than the one being added today.

SECOND_HASH = "0123456789abcdef0123456789abcdef"


async def _collision_two_legacy_rows(hass):
    """The #805 shape: one suffix under two generated roots."""
    entry, device, created = await _entry_with_rows(hass, rows=("silence",))
    twin = er.async_get(hass).async_get_or_create(
        "switch",
        "dahua",
        SECOND_HASH + "_silence",
        config_entry=entry,
        device_id=device.id,
        suggested_object_id="spare_monitor_silence",
    )
    return entry, [created[0].entity_id, twin.entity_id]


async def _collision_target_already_held(hass):
    """The report's shape: something already carries the new identity."""
    entry, device, created = await _entry_with_rows(hass, rows=("silence",))
    holder = er.async_get(hass).async_get_or_create(
        "switch",
        "dahua",
        SERIAL + "_silence",
        config_entry=entry,
        device_id=device.id,
        suggested_object_id="spare_monitor_silence",
    )
    return entry, [created[0].entity_id, holder.entity_id]


async def _collision_device_already_held(hass):
    entry, _device, _created = await _entry_with_rows(hass)
    other = MockConfigEntry(domain="dahua", data={"address": "10.0.0.231"})
    other.add_to_hass(hass)
    dr.async_get(hass).async_get_or_create(
        config_entry_id=other.entry_id,
        identifiers={("dahua", SERIAL)},
        name="Spare Monitor",
    )
    return entry, ["Hallway Monitor", "Spare Monitor"]


async def _collision_entry_already_held(hass):
    entry, _device, _created = await _entry_with_rows(hass)
    MockConfigEntry(
        domain="dahua", unique_id=SERIAL, title="Spare Hallway Monitor"
    ).add_to_hass(hass)
    return entry, ["Spare Hallway Monitor"]


COLLISIONS = [
    _collision_two_legacy_rows,
    _collision_target_already_held,
    _collision_device_already_held,
    _collision_entry_already_held,
]


@pytest.mark.parametrize("build", COLLISIONS, ids=lambda f: f.__name__[11:])
async def test_a_refusal_names_everything_somebody_would_act_on(hass, build):
    entry, expected = await build(hass)

    with pytest.raises(ValueError) as refused:
        await migrate.async_adopt_the_real_serial(hass, entry, [_coordinator()])

    said = str(refused.value)
    assert [name for name in expected if name not in said] == [], said


@pytest.mark.parametrize("build", COLLISIONS, ids=lambda f: f.__name__[11:])
async def test_no_refusal_ever_prints_an_identity(hass, build):
    """⚠ The generated identity is an md5 of the credentials, and these
    strings are shown on the UI and pasted into issues."""
    entry, _expected = await build(hass)

    with pytest.raises(ValueError) as refused:
        await migrate.async_adopt_the_real_serial(hass, entry, [_coordinator()])

    said = str(refused.value)
    assert HASH not in said, said
    assert SECOND_HASH not in said, said
    assert SERIAL not in said, said


# --- a row nothing is behind does not get to stop the upgrade (#1143) -------
#
# @wiiNinja could not get from 1.3.1 to 2.0 at all. The script on the issue
# read his registry: 207 Dahua rows, one entry, 44 identities wanted by two
# rows each, and all 44 the same shape.
#
#     binary_sensor.nvrgarage_motion_alarm      32 hex root   disabled by its device
#     binary_sensor.nvrgarage_motion_alarm_2    real serial   live
#
# 44 out of 44, both ways: not one disabled row on a serial, not one live row
# on a generated identity. The duplication had already happened, long before
# this migration existed, and his automations were on the live `_2` names and
# working.
#
# So it was refusing to protect entities that were in no danger, and the
# entry retried for ever. He deleted one row by hand, which cleared one
# collision out of 44 and changed nothing he could see.
#
# Refusing is still right wherever a live row is at stake, which is what the
# conflict tests above hold. This is the case where it buys nothing.


def _row(entities, entry, device, unique_id, object_id):
    return entities.async_get_or_create(
        "switch",
        "dahua",
        unique_id,
        config_entry=entry,
        device_id=device.id,
        suggested_object_id=object_id,
    )


async def _two_devices(hass):
    """The dead device and the one that replaced it, as #1143 has them."""
    entry, dead_device, _created = await _entry_with_rows(hass, rows=())
    live_device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={("dahua", SERIAL)},
        name="NVR Garage",
    )
    return entry, dead_device, live_device


async def _switch_off(hass, device):
    """Disable a device, and let that reach its entities.

    ⚠ Two things cost a red run each. A row created with
    `disabled_by=DEVICE` on an ENABLED device comes back enabled, and
    disabling the device before the rows exist does not reach rows added
    afterwards: the flag arrives through the device update, so the rows have
    to be there first and the update has to be awaited. A fixture that gets
    either wrong quietly tests the opposite case.
    """
    dr.async_get(hass).async_update_device(
        device.id, disabled_by=dr.DeviceEntryDisabler.USER
    )
    await hass.async_block_till_done()


async def _superseded_pair(hass, suffix="silence"):
    """@wiiNinja's shape: a dead generated row beside the live serial one."""
    entry, dead_device, live_device = await _two_devices(hass)
    entities = er.async_get(hass)
    dead = _row(
        entities,
        entry,
        dead_device,
        "{0}_{1}".format(HASH, suffix),
        "nvrgarage_{0}".format(suffix),
    )
    live = _row(
        entities,
        entry,
        live_device,
        "{0}_{1}".format(SERIAL, suffix),
        "nvrgarage_{0}_2".format(suffix),
    )
    await _switch_off(hass, dead_device)
    assert entities.async_get(dead.entity_id).disabled_by, "fixture is not the shape"
    assert not entities.async_get(live.entity_id).disabled_by
    return entry, live_device, dead, live


async def test_a_dead_row_blocked_by_a_live_one_does_not_stop_the_migration(hass):
    """The report. Everything below is a property of this one working."""
    entry, _live_device, _dead, _live = await _superseded_pair(hass)

    await migrate.async_adopt_the_real_serial(hass, entry, [_coordinator()])

    assert entry.unique_id == SERIAL, "the entry never got off the old identity"


async def test_the_row_left_behind_is_left_exactly_as_it_was(hass):
    """Passed over, not deleted and not altered. Clearing it is a separate
    card with somebody pressing the button."""
    entry, _live_device, dead, _live = await _superseded_pair(hass)

    await migrate.async_adopt_the_real_serial(hass, entry, [_coordinator()])

    still = er.async_get(hass).async_get(dead.entity_id)
    assert still is not None, "deleted a row the user never agreed to lose"
    assert still.unique_id == HASH + "_silence"
    assert still.disabled_by, "re-enabled a row it was only meant to pass over"


async def test_the_live_row_keeps_the_identity_it_already_had(hass):
    entry, _live_device, _dead, live = await _superseded_pair(hass)

    await migrate.async_adopt_the_real_serial(hass, entry, [_coordinator()])

    assert er.async_get(hass).async_get(live.entity_id).unique_id == SERIAL + "_silence"


async def test_everything_that_can_move_still_moves(hass):
    """Passing one row over must not pass the rest over with it."""
    entry, live_device, _dead, _live = await _superseded_pair(hass)
    entities = er.async_get(hass)
    movable = _row(
        entities, entry, live_device, HASH + "_ring_volume", "nvrgarage_ring_volume"
    )

    await migrate.async_adopt_the_real_serial(hass, entry, [_coordinator()])

    moved = entities.async_get(movable.entity_id)
    assert moved.unique_id == SERIAL + "_ring_volume"
    assert moved.entity_id == movable.entity_id, "entity IDs must not change"


async def test_two_dead_rows_still_refuse(hass):
    """Two ghosts say nothing about which is which, so that is a decision
    rather than a tidy-up."""
    entry, dead_device, other_device = await _two_devices(hass)
    entities = er.async_get(hass)
    _row(entities, entry, dead_device, HASH + "_silence", "nvrgarage_silence")
    _row(entities, entry, other_device, SERIAL + "_silence", "nvrgarage_silence_2")
    await _switch_off(hass, dead_device)
    await _switch_off(hass, other_device)

    with pytest.raises(ValueError, match="conflict"):
        await migrate.async_adopt_the_real_serial(hass, entry, [_coordinator()])


async def test_a_live_row_that_cannot_move_still_refuses(hass):
    """The control, stated here rather than left to the tests above: an
    enabled row is a live entity and its fate is not a tidy-up."""
    entry, device, created = await _entry_with_rows(hass, rows=("silence",))
    entities = er.async_get(hass)
    _row(entities, entry, device, SERIAL + "_silence", "nvrgarage_silence_2")

    with pytest.raises(ValueError, match="conflict"):
        await migrate.async_adopt_the_real_serial(hass, entry, [_coordinator()])

    assert entities.async_get(created[0].entity_id).unique_id == HASH + "_silence"


# --- and the same question one level up, on devices -------------------------
#
# His rows are disabled BY THEIR DEVICE, so the dead device is in the registry
# too, and passing over only the entity rows would move the refusal rather
# than remove it.


async def test_a_dead_device_superseded_by_a_live_one_is_passed_over(hass):
    entry, dead, live = await _two_devices(hass)
    await _switch_off(hass, dead)
    devices = dr.async_get(hass)

    await migrate.async_adopt_the_real_serial(hass, entry, [_coordinator()])

    assert devices.async_get(dead.id).identifiers == {("dahua", HASH)}
    assert devices.async_get(live.id).identifiers == {("dahua", SERIAL)}
    assert entry.unique_id == SERIAL


async def test_a_dead_device_blocked_by_another_dead_one_still_refuses(hass):
    entry, dead, other = await _two_devices(hass)
    await _switch_off(hass, dead)
    await _switch_off(hass, other)

    with pytest.raises(ValueError, match="conflict"):
        await migrate.async_adopt_the_real_serial(hass, entry, [_coordinator()])


async def test_a_registry_that_is_all_residue_reports_nothing_moved(hass):
    """Forty four pairs and nothing else is a setup that simply proceeds."""
    entry, dead_device, live_device = await _two_devices(hass)
    entities = er.async_get(hass)
    for index in range(44):
        _row(
            entities,
            entry,
            dead_device,
            "{0}_{1}".format(HASH, index),
            "nvrgarage_{0}".format(index),
        )
        _row(
            entities,
            entry,
            live_device,
            "{0}_{1}".format(SERIAL, index),
            "nvrgarage_{0}_2".format(index),
        )
    await _switch_off(hass, dead_device)

    moved = await migrate.async_adopt_the_real_serial(hass, entry, [_coordinator()])

    assert moved == 0, "moved %d rows that were all residue" % moved
    assert entry.unique_id == SERIAL
    assert len(er.async_entries_for_config_entry(entities, entry.entry_id)) == 88


# --- and it is decided again after the backup (#1145 review) ----------------
#
# @rroller found that deciding what to pass over BEFORE `_async_backup_once`
# means nothing rechecks it afterwards. The backup is the one await in this
# function, it yields to every other entry setting up, and the second
# `preflight` exists precisely to cover it. A pass-over settled before that
# await skipped straight past the thing guarding it.
#
# Both directions matter, and they fail differently:
#
#   - the dead device is switched back on, so its row is live again
#   - the live holder is switched off, so it is residue too, and which of
#     two ghosts should win is not something to decide quietly
#
# Either way the pass-over is no longer justified, and the answer is the
# refusal, not a migration that went ahead on a stale reading.


async def _during_the_backup(hass, monkeypatch, job):
    """Run `job` inside the one await `async_adopt_the_real_serial` has."""

    async def backup_job(*args):
        await job()
        return "backup"

    monkeypatch.setattr(hass, "async_add_executor_job", backup_job)


async def test_a_dead_row_that_comes_back_during_the_backup_is_not_passed_over(
    hass, monkeypatch
):
    entry, _live_device, dead, _live = await _superseded_pair(hass)
    dead_device_id = er.async_get(hass).async_get(dead.entity_id).device_id

    async def switched_back_on():
        dr.async_get(hass).async_update_device(dead_device_id, disabled_by=None)
        await hass.async_block_till_done()

    await _during_the_backup(hass, monkeypatch, switched_back_on)

    with pytest.raises(ValueError, match="conflict"):
        await migrate.async_adopt_the_real_serial(hass, entry, [_coordinator()])

    assert er.async_get(hass).async_get(dead.entity_id).unique_id == HASH + "_silence"
    assert entry.unique_id == HASH, "migrated on a reading that had stopped being true"


async def test_a_holder_switched_off_during_the_backup_stops_the_pass_over(
    hass, monkeypatch
):
    entry, live_device, dead, _live = await _superseded_pair(hass)

    async def switched_off():
        dr.async_get(hass).async_update_device(
            live_device.id, disabled_by=dr.DeviceEntryDisabler.USER
        )
        await hass.async_block_till_done()

    await _during_the_backup(hass, monkeypatch, switched_off)

    with pytest.raises(ValueError, match="conflict"):
        await migrate.async_adopt_the_real_serial(hass, entry, [_coordinator()])

    assert er.async_get(hass).async_get(dead.entity_id).unique_id == HASH + "_silence"
    assert entry.unique_id == HASH


async def test_a_dead_device_revived_during_the_backup_is_not_passed_over(
    hass, monkeypatch
):
    """The same question one level up, where there are no entity rows at all
    to carry the answer."""
    entry, dead, _live = await _two_devices(hass)
    await _switch_off(hass, dead)

    async def switched_back_on():
        dr.async_get(hass).async_update_device(dead.id, disabled_by=None)
        await hass.async_block_till_done()

    await _during_the_backup(hass, monkeypatch, switched_back_on)

    with pytest.raises(ValueError, match="conflict"):
        await migrate.async_adopt_the_real_serial(hass, entry, [_coordinator()])

    assert dr.async_get(hass).async_get(dead.id).identifiers == {("dahua", HASH)}
    assert entry.unique_id == HASH


async def test_a_pass_over_that_is_still_true_after_the_backup_still_happens(
    hass, monkeypatch
):
    """The control. Rechecking must not turn every pass-over into a refusal."""
    entry, _live_device, dead, _live = await _superseded_pair(hass)

    async def nothing_changes():
        await hass.async_block_till_done()

    await _during_the_backup(hass, monkeypatch, nothing_changes)

    await migrate.async_adopt_the_real_serial(hass, entry, [_coordinator()])

    assert entry.unique_id == SERIAL
    assert er.async_get(hass).async_get(dead.entity_id) is not None
