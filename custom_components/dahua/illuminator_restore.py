"""Persistent recovery state for lighting-scheme illuminator overrides."""

import asyncio

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN

STORAGE_VERSION = 1
STORAGE_KEY = f"{DOMAIN}.illuminator_restore_modes"
SNAPSHOT_STORAGE_KEY = f"{DOMAIN}.channel_lighting_snapshots"
# The per-entity override store. One per illuminator rather than per entry,
# because what it holds is that one channel's own Lighting_V2 row.
OVERRIDE_STORAGE_KEY = f"{DOMAIN}.illuminator_restore"

# The suffix the illuminator entity's unique_id ends with. Named here because
# removing an entry has to find those stores by it, and a literal in two places
# is a literal that can drift.
OVERRIDE_UNIQUE_ID_SUFFIX = "_illuminator"


def override_store_key(entry_id: str, unique_id: str) -> str:
    """Where one illuminator entity keeps the camera state it overrode.

    Built here so the entity that writes it and the removal that clears it
    cannot disagree about the name.
    """
    return "{0}.{1}.{2}".format(OVERRIDE_STORAGE_KEY, entry_id, unique_id)


async def async_forget_entry_storage(hass, entry_id: str, override_unique_ids) -> list:
    """Delete every store this config entry owns. Returns what was outstanding.

    Three kinds: the lighting-scheme restore modes, the channel snapshots, and
    one override store per illuminator entity. All three are keyed by config
    entry id, so removing the entry leaves them unreachable -- a re-add gets a
    new id -- and they sat in `.storage` for ever.

    The return value is the channels whose tables the channel-scoped override
    still held. Those cameras are in WhiteMode, and the snapshot that was the
    only way back is going with the entry, so the caller says so. That was
    already true of a removal; it was just silent.
    """
    snapshots = ChannelLightingSnapshotStore(hass, entry_id)
    outstanding = await snapshots.async_channels()
    await snapshots.async_remove_all()
    await IlluminatorRestoreStore(hass, entry_id).async_remove_all()
    for unique_id in override_unique_ids:
        await Store(
            hass, STORAGE_VERSION, override_store_key(entry_id, unique_id)
        ).async_remove()
    return outstanding


class IlluminatorRestoreStore:
    """Persist original lighting modes while the illuminator owns a profile."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        """Create storage scoped to one stable config-entry identity."""
        # The filename supplies the stable config-entry part of the key; each
        # record inside it is keyed by channel/profile.
        self._hass = hass
        self._store_key = f"{STORAGE_KEY}.{entry_id}"
        self._store = self._new_store()
        self._lock = asyncio.Lock()
        self._data: dict[str, str] | None = None

    def _new_store(self) -> Store:
        """Return a Store instance for this config entry."""
        # This is the only copy of the pre-takeover camera policy. A normal
        # non-atomic replacement could be truncated by a host/process failure
        # while the camera's persistent WhiteMode write survives.
        return Store(
            self._hass,
            STORAGE_VERSION,
            self._store_key,
            atomic_writes=True,
        )

    @staticmethod
    def _mode_key(channel: int, profile: int) -> str:
        return f"{channel}:{profile}"

    async def _async_load_locked(self) -> dict[str, str]:
        if self._data is None:
            loaded = await self._store.async_load()
            if not isinstance(loaded, dict):
                loaded = {}
            modes = loaded.get("modes")
            if not isinstance(modes, dict):
                modes = {}
            self._data = {
                key: mode
                for key, mode in modes.items()
                if isinstance(key, str) and isinstance(mode, str) and mode
            }
        return self._data

    async def _async_save_locked(self, modes: dict[str, str]) -> None:
        """Keep writes serialized until their executor work has finished."""
        save_task = asyncio.create_task(self._store.async_save({"modes": modes}))
        cancelled_error = None
        while not save_task.done():
            try:
                await asyncio.shield(save_task)
            except asyncio.CancelledError as err:
                # Store writes in an executor. Cancelling the await does not
                # stop that worker, so releasing our lock here could let a
                # newer write finish first and then be overwritten by this
                # older one. Repeated cancellations must not break the drain.
                if cancelled_error is None:
                    cancelled_error = err
        save_task.result()
        if cancelled_error is not None:
            raise cancelled_error

    async def async_get(self, channel: int, profile: int) -> str | None:
        """Return the saved mode for one entry/channel/profile."""
        async with self._lock:
            data = await self._async_load_locked()
            return data.get(self._mode_key(channel, profile))

    async def async_keys(self) -> list[tuple[int, int]]:
        """Return valid channel/profile keys with outstanding recovery state."""
        async with self._lock:
            data = await self._async_load_locked()
            result = []
            for key in data:
                try:
                    channel, profile = (int(part) for part in key.split(":"))
                except (TypeError, ValueError):
                    continue
                result.append((channel, profile))
            return result

    async def async_set(self, channel: int, profile: int, mode: str) -> None:
        """Durably save a recovery mode before changing the camera."""
        async with self._lock:
            data = await self._async_load_locked()
            updated = dict(data)
            key = self._mode_key(channel, profile)
            updated[key] = mode
            await self._async_save_locked(updated)
            persisted = await self._load_fresh_modes()
            if persisted.get(key) != mode:
                raise RuntimeError("Could not verify saved illuminator recovery state")
            self._data = updated

    async def async_remove(self, channel: int, profile: int) -> None:
        """Remove a recovery mode after restore or stale-state reconciliation."""
        async with self._lock:
            data = await self._async_load_locked()
            key = self._mode_key(channel, profile)
            if key not in data:
                return

            updated = dict(data)
            del updated[key]
            await self._async_save_locked(updated)
            persisted = await self._load_fresh_modes()
            if key in persisted:
                raise RuntimeError(
                    "Could not verify cleared illuminator recovery state"
                )
            self._data = updated

    async def async_remove_all(self) -> None:
        """Delete the file. For a config entry that is being removed."""
        async with self._lock:
            await self._store.async_remove()
            self._data = None

    async def _load_fresh_modes(self) -> dict[str, str]:
        """Read storage through a fresh Store to verify the on-disk result."""
        loaded = await self._new_store().async_load()
        if not isinstance(loaded, dict) or not isinstance(loaded.get("modes"), dict):
            return {}
        return {
            key: mode
            for key, mode in loaded["modes"].items()
            if isinstance(key, str) and isinstance(mode, str) and mode
        }


class ChannelLightingSnapshotStore:
    """Persist a recorder channel's exact LightingScheme + Lighting_V2 tables
    while the white-light override owns them (#959).

    Forcing the white light on a recorder channel switches the camera's working
    mode to WhiteMode, which disables its own night illumination if left set.
    The only safe undo is writing back the exact tables that were there before,
    so those are kept here. Atomic, because this is the sole recovery copy and
    the failure guarded against is a truncated write leaving the camera in
    WhiteMode with no way back -- the flood-light restart lesson (#983) applied
    to a persistent write. Keyed on channel alone: the override writes every
    scene, so there is no per-profile state to keep. A separate store from
    IlluminatorRestoreStore because that one keeps mode strings and drops a dict
    on load, which would lose a snapshot silently.
    """

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._hass = hass
        self._store_key = f"{SNAPSHOT_STORAGE_KEY}.{entry_id}"
        self._store = self._new_store()
        self._lock = asyncio.Lock()
        self._data: dict[str, dict] | None = None

    def _new_store(self) -> Store:
        return Store(self._hass, STORAGE_VERSION, self._store_key, atomic_writes=True)

    async def _load_locked(self) -> dict[str, dict]:
        if self._data is None:
            loaded = await self._store.async_load()
            snaps = loaded.get("snapshots") if isinstance(loaded, dict) else None
            self._data = {
                key: snap
                for key, snap in (snaps.items() if isinstance(snaps, dict) else [])
                if isinstance(key, str) and isinstance(snap, dict)
            }
        return self._data

    async def async_get(self, channel: int) -> dict | None:
        """The saved tables for one channel, or None if none is held."""
        async with self._lock:
            data = await self._load_locked()
            snap = data.get(str(channel))
            return snap if isinstance(snap, dict) else None

    async def async_channels(self) -> list[int]:
        """Channels with an outstanding snapshot, for startup recovery."""
        async with self._lock:
            data = await self._load_locked()
            out = []
            for key in data:
                try:
                    out.append(int(key))
                except (TypeError, ValueError):
                    continue
            return out

    async def async_set(self, channel: int, snapshot: dict) -> None:
        """Save the exact tables before the override changes the camera."""
        async with self._lock:
            data = await self._load_locked()
            updated = dict(data)
            updated[str(channel)] = snapshot
            await self._save_locked(updated)
            self._data = updated

    async def async_remove(self, channel: int) -> None:
        """Forget a snapshot once the camera has been restored."""
        async with self._lock:
            data = await self._load_locked()
            key = str(channel)
            if key not in data:
                return
            updated = dict(data)
            del updated[key]
            await self._save_locked(updated)
            self._data = updated

    async def async_remove_all(self) -> None:
        """Delete the file. For a config entry that is being removed."""
        async with self._lock:
            await self._store.async_remove()
            self._data = None

    async def _save_locked(self, snapshots: dict[str, dict]) -> None:
        """Serialize writes until the executor work has finished.

        The same drain as IlluminatorRestoreStore: a Store write runs in an
        executor and cancelling the await does not stop it, so releasing the
        lock early could let a newer write finish first and then be overwritten.
        """
        save_task = asyncio.create_task(
            self._store.async_save({"snapshots": snapshots})
        )
        cancelled_error = None
        while not save_task.done():
            try:
                await asyncio.shield(save_task)
            except asyncio.CancelledError as err:
                if cancelled_error is None:
                    cancelled_error = err
        save_task.result()
        if cancelled_error is not None:
            raise cancelled_error
