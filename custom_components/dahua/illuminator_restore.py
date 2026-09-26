"""Persistent recovery state for lighting-scheme illuminator overrides."""

import asyncio

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN

STORAGE_VERSION = 1
STORAGE_KEY = f"{DOMAIN}.illuminator_restore_modes"


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
