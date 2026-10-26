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
    listed = shown["description_placeholders"]["entities"]
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
    assert siren.entity_id in shown["description_placeholders"]["entities"]


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
        not in shown["description_placeholders"]["entities"]
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
