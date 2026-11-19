"""Keep a refused siren refused across a restart (#942).

`refusals.py` learns from the first refusal that a control can never work, and
the switch goes unavailable. That memory used to live only in the process, so
after every restart the switch was offered again and the next press was refused
again. An AD410 refuses `CoaxialControlIO.control` with "Method not found!",
which no restart changes.

What a refusal must still not outlive, and why each holds:

- **A reload.** The warning says "Reload the entry to try again", so unloading
  the entry deletes this file. Home Assistant does not unload entries when it
  stops (`ConfigEntries._async_shutdown` only cancels setup retries, on the
  2026.8.0 minimum as on current core), so a restart keeps it and a reload,
  a disable or a removal does not.
- **A firmware change.** A refusal is stored with the firmware that gave it and
  only restored while the device still reports that firmware, so an upgrade
  that adds the method costs one refusal to discover, as before.

Siren only. Infrared refusals choose a fallback table (`instead=`), and keeping
those would change which table a channel starts on, which is a different
question from whether a control is offered.

One file per entry, holding every channel, written under one lock per entry, so
two channels refused at once cannot overwrite each other.
"""

from __future__ import annotations

import asyncio
import logging

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from . import refusals

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
PERSISTED = frozenset({refusals.SIREN})

_LOCKS: dict[str, asyncio.Lock] = {}


def storage_key(entry_id: str) -> str:
    return f"dahua.refusals.{entry_id}"


def _store(hass: HomeAssistant, entry_id: str) -> Store:
    return Store(hass, STORAGE_VERSION, storage_key(entry_id), atomic_writes=True)


def _lock(entry_id: str) -> asyncio.Lock:
    return _LOCKS.setdefault(entry_id, asyncio.Lock())


def _firmware(coordinator) -> str | None:
    try:
        firmware = coordinator.get_firmware_version()
    except Exception:  # pylint: disable=broad-except
        return None
    return firmware if isinstance(firmware, str) and firmware else None


async def async_restore(hass: HomeAssistant, entry_id: str, coordinators) -> None:
    """Put back what each channel refused under the firmware it still runs.

    Called before the platforms are forwarded, because `available` reads the
    refusal directly: restored later, the switch would start available and then
    flip, which is the thing this exists to stop.
    """
    try:
        data = await _store(hass, entry_id).async_load() or {}
    except Exception:  # pylint: disable=broad-except
        _LOGGER.debug("Could not read stored refusals for %s", entry_id, exc_info=True)
        return
    # The file can read cleanly and still not be the shape written here, and
    # a raise from this function stops the entry forwarding any platform. So
    # anything that is not a mapping is skipped, one channel at a time, and
    # the valid channels beside it are still restored.
    if not isinstance(data, dict):
        _LOGGER.debug("Ignoring stored refusals for %s: not a mapping", entry_id)
        return
    for coordinator in coordinators:
        stored = data.get(str(coordinator.get_channel())) or {}
        if not isinstance(stored, dict):
            _LOGGER.debug(
                "Ignoring stored refusals for %s channel %s: not a mapping",
                entry_id,
                coordinator.get_channel(),
            )
            continue
        firmware = _firmware(coordinator)
        for control, refused_under in stored.items():
            if control in PERSISTED and firmware and refused_under == firmware:
                refusals.recall(coordinator, control)


async def async_save(
    hass: HomeAssistant, entry_id: str, coordinator, control: str
) -> None:
    """Record one channel's refusal beside whatever the others hold."""
    if control not in PERSISTED:
        return
    firmware = _firmware(coordinator)
    if not firmware:
        return
    store = _store(hass, entry_id)
    try:
        async with _lock(entry_id):
            if getattr(coordinator, "retired", False):
                # Its entry is unloading and the file is being removed, so a
                # press answered now must not write it back (#1228 review).
                return
            data = await store.async_load() or {}
            if not isinstance(data, dict):
                data = {}
            row = data.get(str(coordinator.get_channel()))
            if not isinstance(row, dict):
                row = data[str(coordinator.get_channel())] = {}
            row[control] = firmware
            await store.async_save(data)
    except Exception:  # pylint: disable=broad-except
        # Losing this costs one refused press after the next restart, which is
        # what happened before it existed. Never worth failing the caller over.
        _LOGGER.debug("Could not store a refusal for %s", entry_id, exc_info=True)


async def async_remove(hass: HomeAssistant, entry_id: str) -> None:
    """Forget everything stored for this entry: on unload and on removal."""
    try:
        async with _lock(entry_id):
            await _store(hass, entry_id).async_remove()
    except Exception:  # pylint: disable=broad-except
        _LOGGER.debug(
            "Could not remove stored refusals for %s", entry_id, exc_info=True
        )
