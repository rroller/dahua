"""Merge a recorder's per-channel config entries into one entry per device.

A recorder was added as one config entry per channel, so removing a 64 channel
NVR meant 64 deletions and adding one meant 64 trips through the add form (#827).
This moves each host onto a single entry with one subentry per channel, which is
the shape Home Assistant expects from a hub and the shape its own delete button
already understands.

## Why this is safe for entities

Every entity `unique_id` is built from `get_serial_number()`, which is
`{serial}_{channel}` for a channel and the bare serial for channel 0. None of
them derives from the config entry, and `device_info` identifiers are the same
value. So the merge re-parents rows rather than rebuilding them: entity ids
survive, and with them dashboards, automations, scripts, areas, name overrides
and recorder history.

Measured on a real install before this was written: 10 entries, 232 entities and
10 devices merged with every entity id, unique id and device id byte identical.

## Two things that are load bearing

**Entities move before devices.** A device belongs to exactly one config entry
from Home Assistant 2026.8, so moving a device first leaves its entities pointing
at an entry that no longer owns it, and Home Assistant resolves that by deleting
them. Doing it in the intuitive order sent all 232 entities to `deleted_entities`
in testing. Entities first, devices second, and nothing is removed until it owns
nothing.

**This has to run before any platform loads.** `async_update_entity_platform`
refuses an entity that is already loaded, and Home Assistant sets up config
entries concurrently, so a per-entry `async_migrate_entry` could fire while
another channel's entities are live. It runs from `async_setup`, which happens
once and before any entry.

## Reversibility

There is none, so a backup is taken first. An older version cannot read an entry
with subentries, so downgrading is not a route back, and the registries are Home
Assistant's own state rather than ours to reconstruct. The copies are written
before the first mutation and their location is logged at warning level so it is
in the log the user will be reading if something went wrong.

The timing makes the copies accurate rather than approximate: this runs during
`async_setup`, when the registries have just been read from those files and
nothing has modified them yet.
"""
import logging
import os
import shutil
import time

from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir

from . import ISSUE_SIBLINGS_REMAIN
from .const import CONF_ADDRESS, CONF_CHANNEL, CONF_PORT, DOMAIN

_LOGGER: logging.Logger = logging.getLogger(__package__)

# The registry files worth keeping a copy of. Nothing else is touched.
BACKED_UP = (
    "core.config_entries",
    "core.device_registry",
    "core.entity_registry",
)

# Shared with the config flow so a merged recorder and a freshly added one
# look identical, and so a subentry reconfigure flow can be registered for
# one type rather than two.
CHANNEL_SUBENTRY = "channel"


def _channel_of(entry) -> int:
    """This entry's channel index, as an int whatever it was stored as.

    The add flow writes it as a string for extra channels and leaves it absent
    for the first, so both shapes are in the wild.
    """
    try:
        return int(entry.data.get(CONF_CHANNEL, 0) or 0)
    except (TypeError, ValueError):
        return 0


def _address_of(entry) -> str:
    return (entry.data.get(CONF_ADDRESS) or "").strip().rstrip("/").lower()


def _port_of(entry) -> str:
    """Which of the device behind an address this entry is.

    Two Dahua boxes can sit behind one IP on different ports -- a bridge
    forwarding 80/554 to one and 81/555 to another, which the rest of the
    integration already treats as two devices (client.py's device_key). Grouping
    by address alone folded the second device's channel 0 onto the first device's
    subentry, so its configuration was dropped and its entities were re-parented
    to the wrong camera. The default stands in for an entry that predates the
    field, which is what it was using.
    """
    return str(entry.data.get(CONF_PORT) or "80")


def _subentry_unique_id(address: str, channel: int) -> str:
    return "%s_%d" % (address, channel)


def _backup(hass: HomeAssistant) -> str | None:
    """Copy the registries somewhere the user can find them. Blocking."""
    storage = hass.config.path(".storage")
    target = hass.config.path(
        "dahua-pre-merge-backup-%s" % time.strftime("%Y%m%d-%H%M%S"))
    copied = []
    try:
        os.makedirs(target, exist_ok=True)
        for name in BACKED_UP:
            source = os.path.join(storage, name)
            if os.path.exists(source):
                shutil.copy2(source, os.path.join(target, name))
                copied.append(name)
    except OSError:
        _LOGGER.exception(
            "Could not back up the Home Assistant registries before merging "
            "Dahua entries. The merge has NOT been attempted")
        return None
    if not copied:
        return None
    return target


async def async_merge_channel_entries(hass: HomeAssistant) -> None:
    """Merge every host's per-channel entries into one entry per host.

    Does nothing for a host that already has one entry, which is every single
    camera and every host already merged, so the common case costs one grouping
    pass and no writes at all.
    """
    entries = hass.config_entries.async_entries(DOMAIN)

    by_host: dict[tuple[str, str], list] = {}
    for entry in entries:
        address = _address_of(entry)
        if address:
            by_host.setdefault((address, _port_of(entry)), []).append(entry)

    hosts = {host: group for host, group in by_host.items()
             if len(group) > 1}
    if not hosts:
        return

    backup = await hass.async_add_executor_job(_backup, hass)
    if backup is None:
        return

    _LOGGER.warning(
        "Merging Dahua config entries so each recorder is one device instead of "
        "one entry per channel. %d host(s) affected. Copies of the Home "
        "Assistant registries were saved to %s first; this cannot be undone from "
        "inside Home Assistant, so keep them until you are happy",
        len(hosts), backup)

    for (address, _port), group in hosts.items():
        try:
            await _async_merge_host(hass, address, group)
        except Exception:  # pylint: disable=broad-except
            # One recorder failing must not take the others with it, and must not
            # take setup down: the entries are still individually usable in the
            # shape they are in, which is exactly the shape they were in before.
            _LOGGER.exception(
                "Could not merge the Dahua entries for %s. They are unchanged or "
                "partly merged; the registry copies in %s are the way back",
                address, backup)


async def _async_merge_host(hass: HomeAssistant, address: str, group: list) -> None:
    """Move every channel of one host onto a single entry with subentries."""
    entities = er.async_get(hass)
    devices = dr.async_get(hass)

    ordered = sorted(group, key=_channel_of)
    survivor = ordered[0]

    # Idempotent: a subentry per channel already present means this host has been
    # done, and a half finished run can be re-entered without duplicating any.
    #
    # Added to as the loop goes, not only read. Two entries can claim the same
    # channel of the same host -- a device that reports no serial number gets no
    # unique id, so nothing stopped it being added twice -- and Home Assistant
    # raises on a second subentry with the same unique id. Snapshotting this
    # before the loop meant that raise came from inside the merge, after some
    # entities had already moved. Both entries now fold onto the one channel,
    # which is what they always were.
    existing = {
        subentry.unique_id: subentry_id
        for subentry_id, subentry in survivor.subentries.items()
    }

    subentry_for: dict[str, str] = {}
    for entry in ordered:
        channel = _channel_of(entry)
        unique_id = _subentry_unique_id(address, channel)
        if unique_id in existing:
            subentry_for[entry.entry_id] = existing[unique_id]
            continue
        subentry = ConfigSubentry(
            data=dict(entry.data),
            subentry_type=CHANNEL_SUBENTRY,
            title=entry.title or "Channel %d" % channel,
            unique_id=unique_id,
        )
        hass.config_entries.async_add_subentry(survivor, subentry)
        existing[unique_id] = subentry.subentry_id
        subentry_for[entry.entry_id] = subentry.subentry_id

    # Counted before anything moves, so the check at the end is against what was
    # actually there rather than against what we expect to have done.
    expected = sum(
        len(er.async_entries_for_config_entry(entities, entry.entry_id))
        for entry in ordered)

    # Entities first. See the module docstring: moving a device first makes Home
    # Assistant delete the entities that still point at the old entry.
    moved = 0
    for entry in ordered:
        subentry_id = subentry_for[entry.entry_id]
        for row in list(er.async_entries_for_config_entry(entities, entry.entry_id)):
            entities.async_update_entity_platform(
                row.entity_id,
                DOMAIN,
                new_config_entry_id=survivor.entry_id,
                new_config_subentry_id=subentry_id,
            )
            moved += 1

    # Checked here, before a single device is touched, because this is the last
    # moment at which stopping costs nothing. Moving a device whose entities are
    # not already on the survivor is what makes Home Assistant delete them, so a
    # check afterwards can only report the loss whereas this one prevents it.
    # Counted off the registry rather than off the loop counter above. `moved`
    # counts attempts, so it would say everything went fine even if every call
    # had quietly done nothing, which is exactly what a test of this check found.
    on_survivor = len(er.async_entries_for_config_entry(entities, survivor.entry_id))
    if on_survivor < expected:
        _LOGGER.error(
            "Refusing to merge %s: %d of its %d entities are still on their old "
            "entries. No device has been touched and nothing has been removed, "
            "so the entries are exactly as they were. Please report this",
            address, expected - on_survivor, expected)
        return

    for entry in ordered:
        subentry_id = subentry_for[entry.entry_id]
        for device in list(
                dr.async_entries_for_config_entry(devices, entry.entry_id)):
            devices.async_update_device(
                device.id,
                new_config_entry_id=survivor.entry_id,
                new_config_subentry_id=subentry_id,
            )

    # The survivor must now own everything the group owned. This is cheap, and it
    # is the difference between a silent catastrophe and a loud one: when the
    # device and entity moves were the wrong way round in testing, Home Assistant
    # deleted all 232 entities and the only visible sign was a success message
    # claiming their ids were unchanged. Nothing is removed if this does not add
    # up, so the old entries stay and the entities stay with them.
    survived = len(er.async_entries_for_config_entry(entities, survivor.entry_id))
    if survived < expected:
        _LOGGER.error(
            "Merging %s lost entities: it had %d and the merged entry has %d. "
            "Home Assistant removes an entity when its device moves to an entry "
            "that does not own it, so this is not something the integration can "
            "undo. Nothing further will be removed, and the registry copies "
            "taken before the merge are the way back. Please report this",
            address, expected, survived)
        return

    removed = 0
    for entry in ordered[1:]:
        left = er.async_entries_for_config_entry(entities, entry.entry_id)
        if left:
            # Never remove an entry that still owns something: removing it would
            # take those entities and their history with it.
            _LOGGER.error(
                "Not removing the Dahua entry for channel %d of %s: it still owns "
                "%d entities, so the merge is incomplete and removing it would "
                "delete them", _channel_of(entry), address, len(left))
            continue
        await hass.config_entries.async_remove(entry.entry_id)
        removed += 1

    if removed:
        # These removals are the merge's, not the user's, but each one runs the
        # same removal hook a manual deletion does. That hook sees the surviving
        # entry as a sibling and raises the "more Dahua entries still use
        # <address>" card -- whose fix removes every entry at the address, which
        # is the recorder this merge has just finished creating. Nothing is left
        # to offer, so the card it just raised is withdrawn.
        ir.async_delete_issue(
            hass, DOMAIN, ISSUE_SIBLINGS_REMAIN.format(address))

    _LOGGER.warning(
        "%s is now one Dahua entry with %d channels: %d entities moved and %d "
        "redundant entries removed, with all %d accounted for on the merged "
        "entry. Entity ids are unchanged, so dashboards and automations keep "
        "working",
        # The subentries the survivor actually has, not the number of entries
        # that were folded into them: two entries can share a channel, and
        # counting entries would then claim a channel that does not exist.
        address, len(survivor.subentries), moved, removed, survived)
