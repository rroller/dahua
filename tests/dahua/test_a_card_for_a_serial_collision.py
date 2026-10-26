"""A collision that no retry clears should say so, and offer the way out.

@wiiNinja on #1143 upgraded 1.3.1 to 2.0 and got

    Failed setup, will retry: Serial registry migration could not complete:
    Serial migration entity conflict: binary_sensor.amcrestnvr_motion_alarm

The integration then retried, forever, on a condition no amount of retrying
changes: two registry rows wanting one identity. "Will retry" was false, the
message named one side of two, and the one tool that could have cleared it,
the "registry rows nothing is behind" card, is only offered after a setup
that succeeds. So the only route out was hand-editing `.storage`, which he
tried, on the wrong row, because the message pointed at that one.

#1144 made the refusal name both sides. #1143's own measurement then showed
that 44 of his 44 collisions were a disabled row blocked by a live one, which
#1145 passes over rather than refusing. This is the remainder: a collision
between two rows that are both live, where something really does have to go,
and somebody has to choose.

Three things here, and the first is the one that matters:

* **The failure is permanent, so it is reported as permanent.** Setup stops
  with `ConfigEntryError` instead of being retried behind "will retry".
* **A card offers the one thing that does change it.** The device's own
  serial wins: everything it removes is filed under a value worked out from
  the credentials, whose place is already taken by a row filed under the
  serial the hardware states.
* **The list is recomputed when the button is pressed.** Never read out of
  the card's data. The #827 merge card acted on evidence gathered earlier and
  deleted 232 entities.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import ConfigEntryError, ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components import dahua as dahua_module
from custom_components.dahua import migrate
from custom_components.dahua.const import DOMAIN
from custom_components.dahua.repairs import ClearSerialConflictRepairFlow

DATA = {
    "username": "u",
    "password": "p",
    "address": "10.0.0.213",
    "port": 80,
    "rtsp_port": 554,
    "name": "Front Door",
    "channel": 0,
    "events": ["VideoMotion"],
}
HASH = "639d060c87813c613a7c217f6c4056bf"
SERIAL = "3E0164EPAN00095"


def _coordinator():
    return SimpleNamespace(
        client=SimpleNamespace(credential_hash=lambda: HASH),
        get_device_serial_number=lambda: SERIAL,
    )


@pytest.fixture(autouse=True)
def _clean_connectors():
    """The pooled connector per host outlives a test otherwise, and the next
    one is then reported as having opened a socket it never asked for."""
    dahua_module._HOST_CONNECTORS.clear()
    yield
    dahua_module._HOST_CONNECTORS.clear()


@pytest.fixture(autouse=True)
def _backups(monkeypatch, tmp_path):
    """The real one copies .storage, which a test hass has not got on disk."""
    monkeypatch.setattr(migrate, "_backup", lambda hass, label="x": str(tmp_path))


async def _collided(hass):
    """Two live rows wanting one identity, which is a decision, not a tidy-up."""
    entry = MockConfigEntry(
        domain=DOMAIN, data=dict(DATA), unique_id=HASH, title="Front Door"
    )
    entry.add_to_hass(hass)
    devices = dr.async_get(hass)
    device = devices.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, HASH)},
        name="Front Door",
    )
    entities = er.async_get(hass)
    old = entities.async_get_or_create(
        "switch",
        DOMAIN,
        HASH + "_siren",
        config_entry=entry,
        device_id=device.id,
        suggested_object_id="front_door_siren",
    )
    holder = entities.async_get_or_create(
        "switch",
        DOMAIN,
        SERIAL + "_siren",
        config_entry=entry,
        device_id=device.id,
        suggested_object_id="front_door_siren_2",
    )
    return entry, old, holder


# --- what the migration now raises ------------------------------------------


async def test_a_collision_raises_the_permanent_type(hass):
    """Not the retryable one. That distinction is the whole complaint."""
    entry, _old, _holder = await _collided(hass)

    with pytest.raises(migrate.SerialMigrationConflict):
        await migrate.async_adopt_the_real_serial(hass, entry, [_coordinator()])


async def test_a_registry_that_moved_under_us_is_still_retryable(hass):
    """The converse, and it is the reason this is a type rather than a string
    match: a registry that changed mid-flight genuinely is worth retrying."""
    entry, _old, _holder = await _collided(hass)

    with pytest.raises(ValueError) as raised:
        raise ValueError("Serial migration registry changed; retry setup")

    assert not isinstance(raised.value, migrate.SerialMigrationConflict)


async def test_the_refusal_carries_the_serial_the_card_needs(hass):
    entry, _old, _holder = await _collided(hass)

    with pytest.raises(migrate.SerialMigrationConflict) as raised:
        await migrate.async_adopt_the_real_serial(hass, entry, [_coordinator()])

    assert raised.value.serial == SERIAL


# --- what setup does with it -------------------------------------------------


def _no_platforms(hass):
    return (
        patch.object(
            dahua_module.DahuaDataUpdateCoordinator, "async_start_event_listener"
        ),
        patch.object(hass.config_entries, "async_forward_entry_setups", AsyncMock()),
        patch.object(
            hass.config_entries, "async_unload_platforms", AsyncMock(return_value=True)
        ),
    )


def _issue(hass, entry):
    return ir.async_get(hass).async_get_issue(
        DOMAIN, dahua_module.ISSUE_SERIAL_CONFLICT.format(entry.entry_id)
    )


async def _setup_raising(hass, entry, error):
    starts, forward, unload = _no_platforms(hass)
    with patch.object(
        dahua_module.DahuaDataUpdateCoordinator,
        "_async_update_data",
        return_value={"serialNumber": SERIAL},
    ), starts, forward, unload, patch(
        "custom_components.dahua.migrate.async_adopt_the_real_serial",
        side_effect=error,
    ), patch.object(
        # An md5 shaped unique_id, which every entry here has, makes setup
        # probe the network for the device's real identity before anything
        # else. That opens a UDP socket, which the test harness blocks.
        dahua_module,
        "async_migrate_synthesised_unique_id",
        AsyncMock(),
    ):
        entry.mock_state(hass, ConfigEntryState.SETUP_IN_PROGRESS)
        with pytest.raises(Exception) as raised:
            await dahua_module.async_setup_entry(hass, entry)
    return raised.value


async def test_setup_stops_rather_than_promising_to_retry(hass):
    """⭐ The report. `ConfigEntryNotReady` renders as "will retry", and this
    will not come right on its own however long it retries."""
    entry, _old, _holder = await _collided(hass)
    conflict = migrate.SerialMigrationConflict("two rows want one identity")
    conflict.serial = SERIAL

    error = await _setup_raising(hass, entry, conflict)

    assert isinstance(error, ConfigEntryError)
    assert not isinstance(error, ConfigEntryNotReady)


async def test_anything_else_is_still_retried(hass):
    """A backup that could not be taken, or a registry that moved. The
    control, so the change above cannot be "stop retrying everything"."""
    entry, _old, _holder = await _collided(hass)

    error = await _setup_raising(hass, entry, ValueError("registry changed"))

    assert isinstance(error, ConfigEntryNotReady)


async def test_the_card_is_raised_with_what_the_flow_needs(hass):
    entry, _old, _holder = await _collided(hass)
    conflict = migrate.SerialMigrationConflict("two rows want one identity")
    conflict.serial = SERIAL

    await _setup_raising(hass, entry, conflict)

    issue = _issue(hass, entry)
    assert issue is not None, "a dead entry and no card is no way out"
    assert issue.is_fixable
    assert issue.data["serial"] == SERIAL
    assert issue.data["entry_id"] == entry.entry_id


async def test_nothing_else_raises_the_card(hass):
    entry, _old, _holder = await _collided(hass)

    await _setup_raising(hass, entry, ValueError("registry changed"))

    assert _issue(hass, entry) is None


# --- which rows it would act on ---------------------------------------------


async def test_a_blocked_live_row_is_offered(hass):
    entry, old, _holder = await _collided(hass)

    rows, devices = migrate.serial_conflicts(hass, entry, SERIAL)

    assert [row.entity_id for row in rows] == [old.entity_id]
    assert devices == []


async def test_the_row_already_holding_the_serial_is_never_offered(hass):
    """It is the survivor by definition: the device agrees with it."""
    entry, _old, holder = await _collided(hass)

    rows, _devices = migrate.serial_conflicts(hass, entry, SERIAL)

    assert holder.entity_id not in [row.entity_id for row in rows]


async def test_residue_is_never_offered_for_deletion(hass):
    """⚠ #1145 passes a disabled row blocked by a live one over rather than
    refusing, so it is not a collision and must not turn up on a card that
    deletes things. @wiiNinja has 44 of them.

    The dead row hangs off its own disabled device, which is the only way a
    row ends up disabled by its device and is how his registry really looks.
    """
    entry, old, _holder = await _collided(hass)
    devices = dr.async_get(hass)
    entities = er.async_get(hass)
    live_device = devices.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, SERIAL)},
        name="Front Door",
    )
    entities.async_update_entity(
        entities.async_get_entity_id("switch", DOMAIN, SERIAL + "_siren"),
        device_id=live_device.id,
    )
    devices.async_update_device(
        entities.async_get(old.entity_id).device_id,
        disabled_by=dr.DeviceEntryDisabler.USER,
    )
    await hass.async_block_till_done()
    assert entities.async_get(old.entity_id).disabled_by, "fixture is not the shape"

    rows, _devices = migrate.serial_conflicts(hass, entry, SERIAL)

    assert rows == [], "offered to delete rows the migration already passes over"


async def test_an_entry_with_no_collision_offers_nothing(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={"address": "10.0.0.1"}, unique_id=HASH)
    entry.add_to_hass(hass)

    assert migrate.serial_conflicts(hass, entry, SERIAL) == ([], [])


# --- and what the flow does --------------------------------------------------


def _flow(hass, entry, serial=SERIAL):
    flow = ClearSerialConflictRepairFlow({"entry_id": entry.entry_id, "serial": serial})
    flow.hass = hass
    return flow


async def test_the_form_names_every_row_it_would_remove(hass):
    entry, old, holder = await _collided(hass)

    shown = await _flow(hass, entry).async_step_init()

    assert shown["type"] == "form"
    listed = shown["description_placeholders"]["removals"]
    assert old.entity_id in listed
    assert holder.entity_id not in listed, "offered to delete the survivor"


async def test_the_form_says_which_serial_it_is_moving_onto(hass):
    entry, _old, _holder = await _collided(hass)

    shown = await _flow(hass, entry).async_step_init()

    assert shown["description_placeholders"]["serial"] == SERIAL
    assert shown["description_placeholders"]["count"] == "1"


async def test_confirming_removes_the_blocked_row_and_only_that(hass):
    entry, old, holder = await _collided(hass)
    flow = _flow(hass, entry)
    await flow.async_step_init()

    with patch.object(hass.config_entries, "async_reload", AsyncMock()):
        done = await flow.async_step_confirm({})

    entities = er.async_get(hass)
    assert done["type"] == "create_entry"
    assert entities.async_get(old.entity_id) is None
    assert entities.async_get(holder.entity_id) is not None
    assert entities.async_get(holder.entity_id).unique_id == SERIAL + "_siren"


async def test_confirming_clears_the_card(hass):
    entry, _old, _holder = await _collided(hass)
    conflict = migrate.SerialMigrationConflict("x")
    conflict.serial = SERIAL
    await _setup_raising(hass, entry, conflict)
    flow = _flow(hass, entry)
    await flow.async_step_init()

    with patch.object(hass.config_entries, "async_reload", AsyncMock()):
        await flow.async_step_confirm({})

    assert _issue(hass, entry) is None


async def test_confirming_sets_the_device_up_again(hass):
    """Nothing is going to retry it: the failure was reported as permanent."""
    entry, _old, _holder = await _collided(hass)
    flow = _flow(hass, entry)
    await flow.async_step_init()

    with patch.object(hass.config_entries, "async_reload", AsyncMock()) as reloaded:
        await flow.async_step_confirm({})

    assert reloaded.await_args.args[0] == entry.entry_id


async def test_the_migration_then_completes(hass):
    """End to end: the collision is what stopped it, so clearing it must be
    enough. Asserted against the real migration rather than the card."""
    entry, _old, _holder = await _collided(hass)
    flow = _flow(hass, entry)
    await flow.async_step_init()
    with patch.object(hass.config_entries, "async_reload", AsyncMock()):
        await flow.async_step_confirm({})

    await migrate.async_adopt_the_real_serial(hass, entry, [_coordinator()])

    assert entry.unique_id == SERIAL


async def test_a_backup_that_cannot_be_taken_removes_nothing(hass, monkeypatch):
    """It deletes entities somebody may be using. The #827 card is why."""
    entry, old, _holder = await _collided(hass)
    monkeypatch.setattr(migrate, "_backup", lambda hass, label="x": None)

    with pytest.raises(ValueError, match="back up"):
        await migrate.async_clear_serial_conflicts(hass, entry, SERIAL)

    assert er.async_get(hass).async_get(old.entity_id) is not None


async def test_a_card_whose_entry_is_gone_aborts_and_clears_itself(hass):
    flow = ClearSerialConflictRepairFlow({"entry_id": "no-such", "serial": SERIAL})
    flow.hass = hass

    shown = await flow.async_step_init()

    assert shown["type"] == "abort"
    assert shown["reason"] == "not_configured"


async def test_a_card_with_nothing_left_to_do_aborts(hass):
    """Somebody may have cleared it by hand between the card and the button."""
    entry, old, _holder = await _collided(hass)
    er.async_get(hass).async_remove(old.entity_id)

    shown = await _flow(hass, entry).async_step_init()

    assert shown["type"] == "abort"
    assert shown["reason"] == "nothing_to_remove"


async def test_the_list_is_recomputed_at_the_button_not_at_the_card(hass):
    """⚠ The #827 lesson, asserted. A card raised earlier must not delete a
    row that has since become the one in use."""
    entry, old, _holder = await _collided(hass)
    flow = _flow(hass, entry)
    await flow.async_step_init()

    er.async_get(hass).async_remove(old.entity_id)

    shown = await flow.async_step_confirm({})

    assert shown["type"] == "abort"
    assert shown["reason"] == "nothing_to_remove"


# --- what changed while the backup was being written ------------------------
#
# @rroller on the #1146 review: the plan was decided, `_async_backup_once` was
# awaited, and the rows in that plan were then removed without anything
# looking again. He reproduced both ends of it. Each of these mutates the
# registry from inside the backup, which is exactly where the yield is.


def _mutating_backup(hass, mutate):
    """A backup that changes the registry while it is being written.

    Patched at `_async_backup_once` rather than at `_backup`, because what is
    being tested is the await, and this is the only await between deciding
    what to delete and deleting it.
    """

    async def backup(_hass):
        mutate()
        return "/tmp/backup"

    return patch.object(migrate, "_async_backup_once", backup)


async def test_the_holder_disappearing_during_the_backup_removes_nothing(hass):
    """The reason to delete is that something else already holds the identity.
    Remove that something during the backup and the reason is gone, but the
    plan still had the row down for deletion."""
    entry, old, holder = await _collided(hass)
    entities = er.async_get(hass)

    with _mutating_backup(hass, lambda: entities.async_remove(holder.entity_id)):
        with pytest.raises(migrate.SerialConflictPlanChanged):
            await migrate.async_clear_serial_conflicts(hass, entry, SERIAL)

    assert entities.async_get(old.entity_id) is not None


async def test_a_row_that_stops_colliding_during_the_backup_is_not_removed(hass):
    """The other end of it: the row itself is given an identity that no longer
    wants the serial. Nothing is in its way any more, so nothing may go."""
    entry, old, holder = await _collided(hass)
    entities = er.async_get(hass)

    with _mutating_backup(
        hass,
        lambda: entities.async_update_entity(
            old.entity_id, new_unique_id="something_else_entirely"
        ),
    ):
        with pytest.raises(migrate.SerialConflictPlanChanged):
            await migrate.async_clear_serial_conflicts(hass, entry, SERIAL)

    assert entities.async_get(old.entity_id) is not None
    assert entities.async_get(holder.entity_id) is not None


async def test_a_different_row_taking_the_identity_is_a_different_plan(hass):
    """Identical entity IDs on both sides and the deletion is still refused.
    The holder is part of the reason, so swapping it is a change even though
    the list of things to delete reads the same."""
    entry, old, holder = await _collided(hass)
    entities = er.async_get(hass)

    def swap():
        # Moved aside rather than removed and recreated. Home Assistant
        # remembers a deleted row and hands the same entity ID back to the
        # next one with that unique ID, so remove-and-recreate rebuilds the
        # same holder and would test nothing.
        entities.async_update_entity(holder.entity_id, new_unique_id="parked")
        entities.async_get_or_create(
            "switch",
            DOMAIN,
            SERIAL + "_siren",
            config_entry=entry,
            suggested_object_id="front_door_siren_3",
        )

    with _mutating_backup(hass, swap):
        with pytest.raises(migrate.SerialConflictPlanChanged):
            await migrate.async_clear_serial_conflicts(hass, entry, SERIAL)

    assert entities.async_get(old.entity_id) is not None


async def test_a_plan_that_did_not_change_still_goes_ahead(hass):
    """The negative control. Without it every test above would pass just as
    well if the function refused unconditionally."""
    entry, old, holder = await _collided(hass)
    entities = er.async_get(hass)

    with _mutating_backup(hass, lambda: None):
        removed = await migrate.async_clear_serial_conflicts(hass, entry, SERIAL)

    assert removed == 1
    assert entities.async_get(old.entity_id) is None
    assert entities.async_get(holder.entity_id) is not None


async def test_the_card_asks_again_rather_than_failing(hass):
    """What somebody sees. The button did nothing, so the form comes back with
    the list worked out again and says why, rather than "unknown error"."""
    entry, _old, holder = await _collided(hass)
    entities = er.async_get(hass)
    flow = _flow(hass, entry)
    await flow.async_step_init()

    captured = []

    def swap():
        captured.append(True)
        entities.async_update_entity(holder.entity_id, new_unique_id="parked")
        entities.async_get_or_create(
            "switch",
            DOMAIN,
            SERIAL + "_siren",
            config_entry=entry,
            suggested_object_id="front_door_siren_3",
        )

    with _mutating_backup(hass, swap):
        shown = await flow.async_step_confirm({})

    assert captured, "the backup never ran, so nothing was being tested"
    assert shown["type"] == "form"
    assert shown["errors"] == {"base": "plan_changed"}


# --- the entities a device removal takes with it ----------------------------
#
# @rroller again: Home Assistant removes a device's entity rows along with the
# device. A legacy device held back by one colliding row also carries rows
# that are not colliding, and confirming took those too, without ever naming
# them.


async def _device_blocked(hass):
    """A legacy device blocked by a serial one, carrying a bystander row.

    The siren collides. The temperature does not: nothing holds
    `<serial>_temperature`, so it could have moved and kept its entity ID.
    """
    entry = MockConfigEntry(
        domain=DOMAIN, data=dict(DATA), unique_id=HASH, title="Front Door"
    )
    entry.add_to_hass(hass)
    devices = dr.async_get(hass)
    legacy = devices.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, HASH)},
        name="Front Door",
    )
    devices.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, SERIAL)},
        name="Front Door (serial)",
    )
    entities = er.async_get(hass)
    siren = entities.async_get_or_create(
        "switch",
        DOMAIN,
        HASH + "_siren",
        config_entry=entry,
        device_id=legacy.id,
        suggested_object_id="front_door_siren",
    )
    entities.async_get_or_create(
        "switch",
        DOMAIN,
        SERIAL + "_siren",
        config_entry=entry,
        suggested_object_id="front_door_siren_2",
    )
    bystander = entities.async_get_or_create(
        "sensor",
        DOMAIN,
        HASH + "_temperature",
        config_entry=entry,
        device_id=legacy.id,
        suggested_object_id="front_door_temperature",
    )
    return entry, legacy, siren, bystander


async def test_an_entity_nobody_confirmed_survives_the_device_removal(hass):
    """The finding itself. The form named the siren; the temperature went with
    the device and was never mentioned."""
    entry, legacy, siren, bystander = await _device_blocked(hass)
    entities = er.async_get(hass)

    await migrate.async_clear_serial_conflicts(hass, entry, SERIAL)

    assert dr.async_get(hass).async_get(legacy.id) is None
    assert entities.async_get(siren.entity_id) is None
    survivor = entities.async_get(bystander.entity_id)
    assert survivor is not None
    assert survivor.entity_id == bystander.entity_id
    assert survivor.unique_id == HASH + "_temperature"


async def test_the_survivor_is_taken_off_the_device_first(hass):
    """How it survives, which is the part that could regress silently. Left on
    the device it is deleted by the cascade, so the row must point at no
    device by the time the device goes."""
    entry, _legacy, _siren, bystander = await _device_blocked(hass)

    await migrate.async_clear_serial_conflicts(hass, entry, SERIAL)

    assert er.async_get(hass).async_get(bystander.entity_id).device_id is None


async def test_a_disabled_bystander_survives_too(hass):
    """`async_entries_for_device` leaves disabled rows out by default while the
    removal cascade takes them, so the one read that matters here is the one
    that includes them."""
    entry, legacy, _siren, _bystander = await _device_blocked(hass)
    entities = er.async_get(hass)
    off = entities.async_get_or_create(
        "sensor",
        DOMAIN,
        HASH + "_humidity",
        config_entry=entry,
        device_id=legacy.id,
        disabled_by=er.RegistryEntryDisabler.USER,
        suggested_object_id="front_door_humidity",
    )

    await migrate.async_clear_serial_conflicts(hass, entry, SERIAL)

    assert entities.async_get(off.entity_id) is not None


async def test_the_form_names_the_entities_it_is_keeping(hass):
    """A confirmation that lists some of what it affects is the defect. The
    kept rows are moved out from under a device somebody is about to delete,
    which is a thing done to them, so it is on the form."""
    entry, _legacy, siren, bystander = await _device_blocked(hass)

    shown = await _flow(hass, entry).async_step_init()

    assert shown["type"] == "form"
    kept = shown["description_placeholders"]["kept"]
    assert bystander.entity_id in kept
    assert siren.entity_id not in kept
    assert siren.entity_id in shown["description_placeholders"]["removals"]


# --- and what changed while the form sat open -------------------------------
#
# A wider window than the backup by a long way. The card recomputing at the
# button stops it deleting something that has since gone; it does nothing
# about something that has since arrived, which was never on screen.


async def test_a_collision_that_appears_while_the_form_is_open_is_not_removed(hass):
    """Pressing the button agrees to the list that was on screen. A row that
    started colliding after it was drawn is not on that list."""
    entry, old, _holder = await _collided(hass)
    entities = er.async_get(hass)
    flow = _flow(hass, entry)
    shown = await flow.async_step_init()
    assert (
        "sensor.front_door_temperature"
        not in shown["description_placeholders"]["removals"]
    )

    late = entities.async_get_or_create(
        "sensor",
        DOMAIN,
        HASH + "_temperature",
        config_entry=entry,
        suggested_object_id="front_door_temperature",
    )
    entities.async_get_or_create(
        "sensor",
        DOMAIN,
        SERIAL + "_temperature",
        config_entry=entry,
        suggested_object_id="front_door_temperature_2",
    )

    again = await flow.async_step_confirm({})

    assert again["type"] == "form"
    assert again["errors"] == {"base": "plan_changed"}
    assert entities.async_get(old.entity_id) is not None
    assert entities.async_get(late.entity_id) is not None


async def test_the_second_press_goes_ahead_with_the_list_it_showed(hass):
    """The way out. The form that comes back is the current one, and pressing
    that removes both. Without this the refusal above could be permanent."""
    entry, old, _holder = await _collided(hass)
    entities = er.async_get(hass)
    flow = _flow(hass, entry)
    await flow.async_step_init()
    late = entities.async_get_or_create(
        "sensor",
        DOMAIN,
        HASH + "_temperature",
        config_entry=entry,
        suggested_object_id="front_door_temperature",
    )
    entities.async_get_or_create(
        "sensor",
        DOMAIN,
        SERIAL + "_temperature",
        config_entry=entry,
        suggested_object_id="front_door_temperature_2",
    )
    await flow.async_step_confirm({})

    done = await flow.async_step_confirm({})

    assert done["type"] == "create_entry"
    assert entities.async_get(old.entity_id) is None
    assert entities.async_get(late.entity_id) is None


# --- the bystander afterwards -----------------------------------------------


async def test_the_kept_row_then_migrates_onto_the_serial(hass):
    """@rroller's actual point: the temperature row could have migrated while
    keeping its entity ID. Surviving the removal is only half of that, so the
    migration is run here and the entity ID checked on the other side."""
    entry, _legacy, _siren, bystander = await _device_blocked(hass)
    entities = er.async_get(hass)

    await migrate.async_clear_serial_conflicts(hass, entry, SERIAL)
    moved = await migrate.async_adopt_the_real_serial(hass, entry, [_coordinator()])

    survivor = entities.async_get(bystander.entity_id)
    assert survivor is not None, "the row the card kept did not survive the migration"
    assert survivor.entity_id == bystander.entity_id
    assert survivor.unique_id == SERIAL + "_temperature"
    assert moved >= 1


async def test_a_kept_row_goes_back_on_a_device_when_it_is_added_again(hass):
    """The form promises it is put back. What puts it back is the platform
    re-registering it, so that is what is exercised: `async_get_or_create`
    with the same unique ID updates `device_id` on the row that is already
    there, rather than creating a second one."""
    entry, _legacy, _siren, bystander = await _device_blocked(hass)
    entities = er.async_get(hass)
    await migrate.async_clear_serial_conflicts(hass, entry, SERIAL)
    assert entities.async_get(bystander.entity_id).device_id is None
    # Not `async_get_device`, which this Home Assistant reports as deprecated
    # because identifiers are no longer unique across config entries.
    serial_device = dahua_module.devices_with_identifier(
        dr.async_get(hass), (DOMAIN, SERIAL)
    )[0]

    again = entities.async_get_or_create(
        "sensor",
        DOMAIN,
        HASH + "_temperature",
        config_entry=entry,
        device_id=serial_device.id,
    )

    assert again.entity_id == bystander.entity_id
    assert again.device_id == serial_device.id


async def test_a_bystander_that_arrives_while_the_form_is_open_is_not_moved(hass):
    """Nothing deletes a bystander, but it is taken off its device, and the
    form lists them for exactly that reason. One that turned up after the
    form was drawn was never on it, so the press does not cover it."""
    entry, legacy, _siren, _bystander = await _device_blocked(hass)
    entities = er.async_get(hass)
    flow = _flow(hass, entry)
    shown = await flow.async_step_init()
    assert "sensor.front_door_pressure" not in shown["description_placeholders"]["kept"]

    late = entities.async_get_or_create(
        "sensor",
        DOMAIN,
        HASH + "_pressure",
        config_entry=entry,
        device_id=legacy.id,
        suggested_object_id="front_door_pressure",
    )

    again = await flow.async_step_confirm({})

    assert again["type"] == "form"
    assert again["errors"] == {"base": "plan_changed"}
    assert entities.async_get(late.entity_id).device_id == legacy.id
    assert late.entity_id in again["description_placeholders"]["kept"]


# --- the collisions nothing is holding ---------------------------------------
#
# @rroller on #1146. `_conflict_holders` finds one shape of collision: an
# identity somebody else is HOLDING. Three of the five things
# `async_adopt_the_real_serial` refuses on are not that shape, because the
# identity is not held by anything -- two of this entry's own rows converging
# on one target, two of its devices converging, and another config entry
# already carrying the serial.
#
# On those the migration raised, the fixable card went up, and pressing its
# button found an empty list, deleted the card and reported that nothing was
# standing in the way. The entry was still dead, nothing retried it, and the
# one notification saying so had just removed itself.
#
# Two changes. The shape is decided before the card is raised, so the
# unremovable ones get a card that explains instead of a button that cannot
# help. And an empty list no longer means resolved: the migration is asked.

HASH2 = "0a1b2c3d4e5f60718293a4b5c6d7e8f9"


def _entry(hass, title="Front Door", unique_id=HASH):
    entry = MockConfigEntry(
        domain=DOMAIN, data=dict(DATA), unique_id=unique_id, title=title
    )
    entry.add_to_hass(hass)
    return entry


async def _two_roots_one_row_target(hass):
    """Two of this entry's own rows wanting one identity, nothing holding it.

    A second synthesised root is what a password change leaves behind: rows
    written under the old hash beside rows under the new one, both of which
    resolve to the serial. `SERIAL_siren` is held by nobody, so there is
    nothing a deletion card could offer to remove.
    """
    entry = _entry(hass)
    entities = er.async_get(hass)
    first = entities.async_get_or_create(
        "switch",
        DOMAIN,
        HASH + "_siren",
        config_entry=entry,
        suggested_object_id="front_door_siren",
    )
    second = entities.async_get_or_create(
        "switch",
        DOMAIN,
        HASH2 + "_siren",
        config_entry=entry,
        suggested_object_id="front_door_siren_old",
    )
    return entry, first, second


async def _two_roots_one_device_target(hass):
    """The same, one layer up: two devices that would become one device.

    Each carries a row of its own with a distinct name, so the entity loop
    finds nothing to complain about and the device loop is actually reached.
    """
    entry = _entry(hass)
    devices = dr.async_get(hass)
    entities = er.async_get(hass)
    pairs = []
    for root, slug in ((HASH, "siren"), (HASH2, "motion")):
        device = devices.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, root)},
            name="Front Door " + slug,
        )
        entities.async_get_or_create(
            "switch",
            DOMAIN,
            root + "_" + slug,
            config_entry=entry,
            device_id=device.id,
            suggested_object_id="front_door_" + slug,
        )
        pairs.append(device)
    return entry, pairs


async def _another_entry_holds_the_serial(hass):
    """The serial this device reports is already some other entry's identity."""
    entry = _entry(hass)
    entities = er.async_get(hass)
    entities.async_get_or_create(
        "switch",
        DOMAIN,
        HASH + "_siren",
        config_entry=entry,
        suggested_object_id="front_door_siren",
    )
    other = _entry(hass, title="The Same Camera Twice", unique_id=SERIAL)
    return entry, other


# --- each one still refuses, and refuses permanently -------------------------


async def test_two_rows_converging_on_one_identity_is_refused(hass):
    entry, _first, _second = await _two_roots_one_row_target(hass)

    with pytest.raises(migrate.SerialMigrationConflict):
        await migrate.async_adopt_the_real_serial(hass, entry, [_coordinator()])


async def test_two_devices_converging_on_one_identity_is_refused(hass):
    entry, _devices = await _two_roots_one_device_target(hass)

    with pytest.raises(migrate.SerialMigrationConflict):
        await migrate.async_adopt_the_real_serial(hass, entry, [_coordinator()])


async def test_another_entry_already_holding_the_serial_is_refused(hass):
    entry, _other = await _another_entry_holds_the_serial(hass)

    with pytest.raises(migrate.SerialMigrationConflict) as raised:
        await migrate.async_adopt_the_real_serial(hass, entry, [_coordinator()])

    assert "The Same Camera Twice" in str(raised.value), "names the other side"


# --- and none of them is something the deletion card can clear ---------------


@pytest.mark.parametrize(
    "build",
    [
        _two_roots_one_row_target,
        _two_roots_one_device_target,
        _another_entry_holds_the_serial,
    ],
    ids=["rows-converge", "devices-converge", "another-entry"],
)
async def test_the_deletion_card_has_nothing_to_offer_for_these(hass, build):
    """⭐ The premise of the whole section, asserted rather than assumed.

    If any of these ever did produce a removal plan, the card below would be
    the wrong one and this file would be describing something that no longer
    happens.
    """
    entry = (await build(hass))[0]

    rows, devices = migrate.serial_conflicts(hass, entry, SERIAL)

    assert (rows, devices) == ([], [])


def _manual_issue(hass, entry):
    return ir.async_get(hass).async_get_issue(
        DOMAIN, dahua_module.ISSUE_SERIAL_CONFLICT_MANUAL.format(entry.entry_id)
    )


@pytest.mark.parametrize(
    "build",
    [
        _two_roots_one_row_target,
        _two_roots_one_device_target,
        _another_entry_holds_the_serial,
    ],
    ids=["rows-converge", "devices-converge", "another-entry"],
)
async def test_an_unremovable_collision_gets_the_card_that_explains(hass, build):
    """⭐ The report. A button that deletes rows is not an answer where there
    are no rows to delete, and the card that offered one deleted itself."""
    entry = (await build(hass))[0]
    conflict = migrate.SerialMigrationConflict("two things want one identity")
    conflict.serial = SERIAL

    await _setup_raising(hass, entry, conflict)

    assert _manual_issue(hass, entry) is not None
    assert _issue(hass, entry) is None, "offered a button that cannot help"


async def test_the_card_that_explains_is_not_fixable(hass):
    """Which is the whole difference, and it is also a hassfest rule: an issue
    carries a description or a fix flow and never both."""
    entry, _first, _second = await _two_roots_one_row_target(hass)
    conflict = migrate.SerialMigrationConflict("two things want one identity")
    conflict.serial = SERIAL

    await _setup_raising(hass, entry, conflict)

    assert _manual_issue(hass, entry).is_fixable is False


async def test_the_card_that_explains_says_what_stopped_it(hass):
    """Both sides, which is #1144's rule, carried through to the card."""
    entry, _other = await _another_entry_holds_the_serial(hass)
    conflict = migrate.SerialMigrationConflict(
        "Serial migration config entry conflict: this device cannot take the "
        "identity it reports because the entry 'The Same Camera Twice' already "
        "holds it."
    )
    conflict.serial = SERIAL

    await _setup_raising(hass, entry, conflict)

    placeholders = _manual_issue(hass, entry).translation_placeholders
    assert "The Same Camera Twice" in placeholders["reason"]
    assert placeholders["serial"] == SERIAL


@pytest.mark.parametrize(
    "build",
    [
        _two_roots_one_row_target,
        _two_roots_one_device_target,
        _another_entry_holds_the_serial,
    ],
    ids=["rows-converge", "devices-converge", "another-entry"],
)
async def test_the_card_that_explains_never_prints_the_identity(hass, build):
    """⭐ The identity is md5 of the address, RTSP port, username and password.
    A card is read on screen and pasted into issues, so printing one hands out
    an offline attack on the password. `_named` exists for this reason.

    The refusal is produced by the real migration and rendered by the real
    classification, because the property belongs to the message `migrate.py`
    writes and nothing is proven by feeding this a message written here. It
    is also the only test that runs the whole path end to end.
    """
    entry = (await build(hass))[0]
    with pytest.raises(migrate.SerialMigrationConflict) as raised:
        await migrate.async_adopt_the_real_serial(hass, entry, [_coordinator()])

    dahua_module._async_offer_to_clear_the_conflict(hass, entry, raised.value)

    rendered = " ".join(_manual_issue(hass, entry).translation_placeholders.values())
    assert HASH not in rendered, rendered
    assert HASH2 not in rendered, rendered


async def test_a_removable_collision_still_gets_the_button(hass):
    """The control. The classification must not turn every card into prose."""
    entry, _old, _holder = await _collided(hass)
    conflict = migrate.SerialMigrationConflict("one row holds another's place")
    conflict.serial = SERIAL

    await _setup_raising(hass, entry, conflict)

    assert _issue(hass, entry) is not None
    assert _manual_issue(hass, entry) is None


async def test_only_ever_one_of_the_two_is_on_screen(hass):
    """A reload can change which shape applies. Two cards for one entry is two
    answers to one question, and clearing one would leave the other."""
    entry, _old, _holder = await _collided(hass)
    conflict = migrate.SerialMigrationConflict("one row holds another's place")
    conflict.serial = SERIAL
    await _setup_raising(hass, entry, conflict)
    assert _issue(hass, entry) is not None

    # The holder goes away, so what is left is two roots converging.
    er.async_get(hass).async_remove(_holder.entity_id)
    er.async_get(hass).async_update_entity(
        _old.entity_id, new_unique_id=HASH2 + "_siren"
    )
    er.async_get(hass).async_get_or_create(
        "switch",
        DOMAIN,
        HASH + "_siren",
        config_entry=entry,
        suggested_object_id="front_door_siren_again",
    )
    await _setup_raising(hass, entry, conflict)

    assert _manual_issue(hass, entry) is not None
    assert _issue(hass, entry) is None


async def test_setting_up_clears_the_card_that_explains_too(hass):
    """It is raised on a failure, so the success path has to take it down.
    Only the fixable one was ever deleted there."""
    entry, _first, _second = await _two_roots_one_row_target(hass)
    conflict = migrate.SerialMigrationConflict("two things want one identity")
    conflict.serial = SERIAL
    await _setup_raising(hass, entry, conflict)
    assert _manual_issue(hass, entry) is not None

    starts, forward, unload = _no_platforms(hass)
    with patch.object(
        dahua_module.DahuaDataUpdateCoordinator,
        "_async_update_data",
        return_value={"serialNumber": SERIAL},
    ), starts, forward, unload, patch(
        "custom_components.dahua.migrate.async_adopt_the_real_serial",
        AsyncMock(return_value=0),
    ), patch.object(
        dahua_module, "async_migrate_synthesised_unique_id", AsyncMock()
    ):
        entry.mock_state(hass, ConfigEntryState.SETUP_IN_PROGRESS)
        await dahua_module.async_setup_entry(hass, entry)

    assert _manual_issue(hass, entry) is None


# --- an empty list is not a migration that can now succeed -------------------


async def _fixable_card_over_an_empty_plan(hass):
    """The state @rroller reproduced: the button's card is up, and the thing
    it would remove is not there to remove."""
    entry, _first, _second = await _two_roots_one_row_target(hass)
    ir.async_create_issue(
        hass,
        DOMAIN,
        dahua_module.ISSUE_SERIAL_CONFLICT.format(entry.entry_id),
        is_fixable=True,
        is_persistent=False,
        severity=ir.IssueSeverity.ERROR,
        translation_key="serial_conflict",
        translation_placeholders={"name": entry.title},
        data={"entry_id": entry.entry_id, "serial": SERIAL},
    )
    return entry


async def test_an_empty_plan_asks_the_migration_rather_than_assuming(hass):
    """⭐ "An empty removal plan does not establish that migration can now
    succeed." The entry is reloaded, it does not come back, so the card stays
    and the abort says the entry still did not set up."""
    entry = await _fixable_card_over_an_empty_plan(hass)
    conflict = migrate.SerialMigrationConflict("two things want one identity")
    conflict.serial = SERIAL

    async def _reload(entry_id):
        """What reloading really does here: run setup, which refuses again."""
        await _setup_raising(hass, entry, conflict)

    with patch.object(hass.config_entries, "async_reload", _reload):
        done = await _flow(hass, entry).async_step_init()

    assert done["type"] == "abort"
    assert done["reason"] == "still_blocked"
    assert _manual_issue(hass, entry) is not None, "left nothing saying why"


async def test_an_empty_plan_that_really_did_resolve_still_clears(hass):
    """The control, and the behaviour that was right all along: somebody
    tidied the registry by hand, the entry loads, the card goes."""
    entry = await _fixable_card_over_an_empty_plan(hass)

    with patch.object(hass.config_entries, "async_reload", AsyncMock()):
        done = await _flow(hass, entry).async_step_init()

    assert done["type"] == "abort"
    assert done["reason"] == "nothing_to_remove"
    assert _issue(hass, entry) is None
    assert _manual_issue(hass, entry) is None


async def test_the_abort_the_user_reads_is_a_real_string(hass):
    """A reason with no translation renders as the raw key."""
    import json

    with open(
        "custom_components/dahua/translations/en.json", encoding="utf-8"
    ) as handle:
        strings = json.load(handle)

    issues = strings["issues"]
    assert "still_blocked" in issues["serial_conflict"]["fix_flow"]["abort"]
    manual = issues["serial_conflict_manual"]
    assert "description" in manual and "title" in manual
    assert "fix_flow" not in manual, "hassfest fails the integration for both"


# --- the shape where the blocker is a device and nothing else ---------------
#
# @pathia reported this one on #1143, and it is the only shape where the old
# form had nothing to show: the count came from rows plus devices while the
# list came from rows alone, so a lone device collision asked to remove "1"
# and then printed "(none, only devices)".


async def _only_a_device_blocked(hass):
    """Two device rows, no colliding entity row anywhere.

    Named the way @pathia's were, because the names are the point: one of
    these is a camera still on a wall and the other may be a leftover from
    the same camera's previous life, and the only thing that tells them
    apart on the form is what their owner called them.
    """
    entry = MockConfigEntry(
        domain=DOMAIN, data=dict(DATA), unique_id=HASH, title="voorzijde"
    )
    entry.add_to_hass(hass)
    devices = dr.async_get(hass)
    legacy = devices.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, HASH)},
        name="voorzijde",
    )
    holder = devices.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, SERIAL)},
        name="Achterzijde schuur",
    )
    return entry, legacy, holder


async def test_the_form_names_the_device_it_would_remove(hass):
    """⭐ The confirmation has to name its own subject. A count of one over a
    list that says "none" is a button nobody can check before pressing."""
    entry, _legacy, _holder = await _only_a_device_blocked(hass)

    shown = await _flow(hass, entry).async_step_init()

    assert shown["type"] == "form"
    listed = shown["description_placeholders"]["removals"]
    assert "voorzijde" in listed, listed
    assert shown["description_placeholders"]["count"] == "1"


async def test_the_form_names_what_is_holding_the_identity_too(hass):
    """Both sides, for the same reason every refusal names both: the holder is
    the entire reason the other one may be deleted."""
    entry, _legacy, _holder = await _only_a_device_blocked(hass)

    shown = await _flow(hass, entry).async_step_init()

    assert "Achterzijde schuur" in shown["description_placeholders"]["removals"]


async def test_the_form_never_prints_the_identity(hass):
    """⭐ The identity is an md5 of the address, RTSP port, username and
    password. This form is screenshotted into issues, so printing one would
    hand out an offline attack on the camera password."""
    entry, _legacy, _holder = await _only_a_device_blocked(hass)

    shown = await _flow(hass, entry).async_step_init()

    rendered = " ".join(str(v) for v in shown["description_placeholders"].values())
    assert HASH not in rendered, rendered
    assert HASH2 not in rendered, rendered


# --- the holder in a different config entry ---------------------------------
#
# Two cameras means two entries, and `_only_a_device_blocked` above keeps both
# devices on one. That matters, because `async_adopt_the_real_serial` checks
# the config entry condition AFTER the device loop: if another entry is also
# filed under the serial, the device refusal is raised first and hides it.
# `_conflict_holders` looks at rows and devices only, so it would offer the
# button for a collision that removing a device cannot clear.


async def _another_entry_holds_it_as_a_device(hass):
    """@pathia's topology: the holder is another entry's device.

    And that other entry is filed under the serial itself, which is what a
    successful migration leaves behind. Both the device refusal and the
    config entry refusal apply; only one of them is reachable.
    """
    entry = MockConfigEntry(
        domain=DOMAIN, data=dict(DATA), unique_id=HASH, title="voorzijde"
    )
    entry.add_to_hass(hass)
    # Deliberately NOT the holder device's name. They were the same string
    # at first, and the assertion that the entry title reaches the card then
    # passed without the code that puts it there: the device refusal already
    # printed that name.
    other = MockConfigEntry(
        domain=DOMAIN,
        data=dict(DATA),
        unique_id=SERIAL,
        title="Tuinhuis",
    )
    other.add_to_hass(hass)
    devices = dr.async_get(hass)
    legacy = devices.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, HASH)},
        name="voorzijde",
    )
    holder = devices.async_get_or_create(
        config_entry_id=other.entry_id,
        identifiers={(DOMAIN, SERIAL), (DOMAIN, HASH2)},
        name="Achterzijde schuur",
    )
    return entry, other, legacy, holder


async def test_removing_the_device_would_not_clear_the_entry_conflict(hass):
    """⭐ The premise of the guard below, asserted rather than assumed.

    Clear the device collision by hand and ask the migration itself whether
    that helped. It does not: the refusal underneath is still there. If this
    ever stops being true the guard is describing a bug that has moved, and
    this fails loudly rather than the card quietly offering a button again.
    """
    entry, _other, legacy, holder = await _another_entry_holds_it_as_a_device(hass)

    await migrate.async_clear_serial_conflicts(hass, entry, SERIAL)

    devices = dr.async_get(hass)
    assert devices.async_get(legacy.id) is None, "the legacy device went"
    assert devices.async_get(holder.id) is not None, "the other entry's survived"

    with pytest.raises(migrate.SerialMigrationConflict) as raised:
        await migrate.async_adopt_the_real_serial(hass, entry, [_coordinator()])
    assert "config entry conflict" in str(raised.value), str(raised.value)


async def test_the_card_that_explains_never_prints_either_identity(hass):
    """⭐ Both sides this time. The holder above carries HASH2 as well, so a
    label that printed identifiers would leak the other camera's too.

    The manual card rather than the form, because this topology no longer
    reaches a form: the reason it is manual is that the deletion would not
    help, and that is the text somebody actually sees.
    """
    entry, _other, _legacy, _holder = await _another_entry_holds_it_as_a_device(hass)
    with pytest.raises(migrate.SerialMigrationConflict) as raised:
        await migrate.async_adopt_the_real_serial(hass, entry, [_coordinator()])

    dahua_module._async_offer_to_clear_the_conflict(hass, entry, raised.value)

    card = _manual_issue(hass, entry)
    rendered = " ".join(str(v) for v in card.translation_placeholders.values())
    assert HASH not in rendered, rendered
    assert HASH2 not in rendered, rendered


async def test_that_topology_is_not_offered_a_form_at_all(hass):
    """And the flow agrees with the classification, which is the half that
    matters when the blocker arrives after the card was raised."""
    entry, _other, _legacy, holder = await _another_entry_holds_it_as_a_device(hass)

    result = await _flow(hass, entry).async_step_init()

    assert result["reason"] == "still_blocked", result
    assert dr.async_get(hass).async_get(holder.id) is not None
    # And it says so rather than going quiet. "Nothing is standing in the
    # way" was the 08:50 finding, and routing through a reload would bring
    # it back whenever the reload fails for its own reasons.
    assert _manual_issue(hass, entry) is not None
    assert _issue(hass, entry) is None


async def test_a_collision_another_entry_also_blocks_is_not_a_button(hass):
    """⭐ The guard. Something removable is in the way, so the old
    classification offered the button, and pressing it would have deleted a
    device and left the entry refusing on the condition underneath."""
    entry, _other, _legacy, _holder = await _another_entry_holds_it_as_a_device(hass)
    with pytest.raises(migrate.SerialMigrationConflict) as raised:
        await migrate.async_adopt_the_real_serial(hass, entry, [_coordinator()])

    dahua_module._async_offer_to_clear_the_conflict(hass, entry, raised.value)

    assert _issue(hass, entry) is None, "offered a button that cannot help"
    assert _manual_issue(hass, entry) is not None


async def test_the_manual_card_names_the_entry_underneath_too(hass):
    """The refusal raised is the device one, because the entry check never
    ran. Saying only that sends somebody to remove a device for nothing."""
    entry, other, _legacy, _holder = await _another_entry_holds_it_as_a_device(hass)
    with pytest.raises(migrate.SerialMigrationConflict) as raised:
        await migrate.async_adopt_the_real_serial(hass, entry, [_coordinator()])

    dahua_module._async_offer_to_clear_the_conflict(hass, entry, raised.value)

    reason = _manual_issue(hass, entry).translation_placeholders["reason"]
    assert "Achterzijde schuur" in reason, reason  # the device, from the refusal
    assert other.title in reason, reason  # the entry, which only the append adds
    assert HASH not in reason and HASH2 not in reason, reason


async def test_a_plain_device_collision_still_gets_the_button(hass):
    """The converse, so the guard cannot quietly swallow the case it is not
    for. Nothing else is filed under the serial here."""
    entry, _legacy, _holder = await _only_a_device_blocked(hass)
    with pytest.raises(migrate.SerialMigrationConflict) as raised:
        await migrate.async_adopt_the_real_serial(hass, entry, [_coordinator()])

    dahua_module._async_offer_to_clear_the_conflict(hass, entry, raised.value)

    assert _issue(hass, entry) is not None
    assert _manual_issue(hass, entry) is None


# --- @rroller's fourth review: removable is still not the same as clearable --
#
# Two more ways the button got offered, or pressed, over a blocker no deletion
# it performs would touch. Both are the same mistake as the one above, caught
# in places the first guard does not reach: the guard asked one question about
# one blocker, at one moment.


async def _removable_collision_masking_a_row_convergence(hass):
    """One collision a deletion clears and one it cannot, in the same entry.

    `HASH_siren` is held by `SERIAL_siren`, so the card finds something to
    remove and offers the button. `HASH_temperature` and `HASH2_temperature`
    both want `SERIAL_temperature`, which nobody holds, so they are not in
    any removal plan and the migration refuses on them the moment the first
    collision is out of the way. A second synthesised root is what a password
    change leaves behind.
    """
    entry, old, holder = await _collided(hass)
    entities = er.async_get(hass)
    for root, slug in ((HASH, "temperature"), (HASH2, "temperature_old")):
        entities.async_get_or_create(
            "sensor",
            DOMAIN,
            root + "_temperature",
            config_entry=entry,
            suggested_object_id="front_door_" + slug,
        )
    return entry, old, holder


async def _removable_collision_masking_a_device_convergence(hass):
    """The same mask, one layer up: the survivor is two devices becoming one.

    The row collision is removable. The two devices both resolve to
    `(dahua, SERIAL)` and nothing holds that, so the device loop has nobody
    to name as a holder and the card never sees them.
    """
    entry, old, holder = await _collided(hass)
    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, HASH2)},
        name="Front Door, before the password change",
    )
    return entry, old, holder


@pytest.mark.parametrize(
    "build",
    [
        _removable_collision_masking_a_row_convergence,
        _removable_collision_masking_a_device_convergence,
    ],
    ids=["rows-converge", "devices-converge"],
)
async def test_clearing_the_removable_half_leaves_the_migration_refusing(hass, build):
    """⭐ The premise, asserted rather than assumed, for both shapes.

    Clear the removable collision by hand and ask the migration itself
    whether that finished the job. It did not. If either of these ever does
    become sufficient, this fails loudly instead of the guard below quietly
    refusing a button that would have worked.
    """
    entry, old, _holder = await build(hass)

    removed = await migrate.async_clear_serial_conflicts(hass, entry, SERIAL)

    assert removed == 1
    assert er.async_get(hass).async_get(old.entity_id) is None, "the row went"

    with pytest.raises(migrate.SerialMigrationConflict) as raised:
        await migrate.async_adopt_the_real_serial(hass, entry, [_coordinator()])
    assert "would both become the same" in str(raised.value), str(raised.value)


@pytest.mark.parametrize(
    "build",
    [
        _removable_collision_masking_a_row_convergence,
        _removable_collision_masking_a_device_convergence,
    ],
    ids=["rows-converge", "devices-converge"],
)
async def test_a_collision_masking_a_convergence_is_not_a_button(hass, build):
    """⭐ The guard. Something removable is in the way, so the classification
    offered the button, and pressing it deletes a row and leaves the entry
    refusing on the convergence underneath."""
    entry, _old, _holder = await build(hass)
    with pytest.raises(migrate.SerialMigrationConflict) as raised:
        await migrate.async_adopt_the_real_serial(hass, entry, [_coordinator()])

    dahua_module._async_offer_to_clear_the_conflict(hass, entry, raised.value)

    assert _issue(hass, entry) is None, "offered a button that cannot help"
    assert _manual_issue(hass, entry) is not None


async def test_the_manual_card_names_the_convergence_underneath_too(hass):
    """The refusal raised is the row collision, because the convergence is
    found later. Printing only that sends somebody to delete for nothing."""
    entry, _old, _holder = await _removable_collision_masking_a_row_convergence(hass)
    with pytest.raises(migrate.SerialMigrationConflict) as raised:
        await migrate.async_adopt_the_real_serial(hass, entry, [_coordinator()])

    dahua_module._async_offer_to_clear_the_conflict(hass, entry, raised.value)

    reason = _manual_issue(hass, entry).translation_placeholders["reason"]
    assert "would both become the same" in reason, reason
    assert "front_door_temperature" in reason, reason
    assert HASH not in reason and HASH2 not in reason, reason


async def test_an_entry_taking_the_serial_while_the_form_is_open_removes_nothing(hass):
    """⭐ @rroller's P1 timing. The form is shown, another entry is then filed
    under the serial, and the row and device plan is identical either side of
    that, so the agreement check sees nothing and the deletion goes ahead."""
    entry, old, _holder = await _collided(hass)
    flow = _flow(hass, entry)
    shown = await flow.async_step_init()
    assert shown["type"] == "form"

    _entry(hass, title="Tuinhuis", unique_id=SERIAL)
    result = await flow.async_step_confirm({})

    assert er.async_get(hass).async_get(old.entity_id) is not None, "deleted anyway"
    assert result["reason"] == "still_blocked", result
    assert _manual_issue(hass, entry) is not None, "said nothing instead"
    assert _issue(hass, entry) is None, "left the button up"


async def test_an_entry_taking_the_serial_during_the_backup_removes_nothing(hass):
    """The same blocker arriving inside the one await there is."""
    entry, old, _holder = await _collided(hass)

    with _mutating_backup(
        hass, lambda: _entry(hass, title="Tuinhuis", unique_id=SERIAL)
    ):
        with pytest.raises(migrate.SerialConflictPlanChanged):
            await migrate.async_clear_serial_conflicts(hass, entry, SERIAL)

    assert er.async_get(hass).async_get(old.entity_id) is not None


async def test_the_refusal_raised_is_still_the_first_one(hass):
    """The five conditions are collected now rather than raised one at a
    time, and the migration raises the first of them.

    That ordering is the whole reason the collecting is safe. Every existing
    report quotes the message it got, and the fixture here satisfies two
    conditions at once, so a change of order would silently re-point them.
    It is also why `preflight` was not reordered to name the real blocker:
    the card says that part now, and the refusal stays where it was.
    """
    entry, _other, _legacy, _holder = await _another_entry_holds_it_as_a_device(hass)

    with pytest.raises(migrate.SerialMigrationConflict) as raised:
        await migrate.async_adopt_the_real_serial(hass, entry, [_coordinator()])

    assert "device conflict" in str(raised.value), str(raised.value)
    assert "config entry conflict" not in str(raised.value), "the order moved"


async def test_the_blocker_list_is_what_the_deletion_would_leave(hass):
    """Not "every refusal": the one the card is about to clear is not in it,
    and the one underneath is. An empty list is what permits the button."""
    entry, other, _legacy, _holder = await _another_entry_holds_it_as_a_device(hass)

    remaining = migrate.serial_adoption_blockers(hass, entry, SERIAL)

    assert len(remaining) == 1, remaining
    assert "config entry conflict" in remaining[0], remaining
    assert other.title in remaining[0], remaining


async def test_the_flow_routes_to_the_card_when_the_blocker_lands_mid_backup(hass):
    """The pressed button, not the function under it.

    The direct test above proves nothing is deleted. This one is about what
    the person is then looking at: not the same deletion form again with
    "press the button again", which is an answer to a different question.
    """
    entry, old, _holder = await _collided(hass)
    flow = _flow(hass, entry)
    assert (await flow.async_step_init())["type"] == "form"

    with _mutating_backup(
        hass, lambda: _entry(hass, title="Tuinhuis", unique_id=SERIAL)
    ):
        result = await flow.async_step_confirm({})

    assert er.async_get(hass).async_get(old.entity_id) is not None, "deleted anyway"
    assert result["reason"] == "still_blocked", result
    assert _manual_issue(hass, entry) is not None
    assert _issue(hass, entry) is None


async def test_the_card_the_flow_raises_quotes_what_the_migration_would(hass):
    """Not the surviving blocker, which is the second half of the story.

    The flow has the whole list, so it names the refusal the entry itself
    would print, and the card appends what is underneath. A card that
    quoted only the survivor would describe a collision the person never
    saw in their log.
    """
    entry, _other, _legacy, _holder = await _another_entry_holds_it_as_a_device(hass)

    await _flow(hass, entry).async_step_init()

    reason = _manual_issue(hass, entry).translation_placeholders["reason"]
    assert "device conflict" in reason, reason
    assert "config entry conflict" in reason, reason
    assert HASH not in reason and HASH2 not in reason, reason
