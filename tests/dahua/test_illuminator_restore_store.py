"""The recovery state that remembers what the camera's lighting was set to.

While the illuminator owns a day/night profile, this store holds the mode that profile
had before the takeover. It is the only copy of that: the camera's own WhiteMode write is
persistent, so losing this record means the camera keeps a setting nobody chose and
nothing knows what to put back. That is why the Store is created with atomic_writes.

Two paths in it had no tests, and both are about reading state written by an older
version or by something that went wrong.

The Store itself is never constructed here. `_async_load_locked` returns `self._data`
whenever it is already set, so setting it directly exercises the real methods without
touching Home Assistant's storage at all.
"""

import asyncio

import pytest

from custom_components.dahua.illuminator_restore import IlluminatorRestoreStore


class _Store:
    """Records what was written, and answers the read-back with what it holds."""

    def __init__(self, on_disk=None):
        self.saved = []
        self._on_disk = dict(on_disk or {})

    async def async_save(self, data):
        self.saved.append(data)
        self._on_disk = dict(data.get("modes") or {})

    async def async_load(self):
        return {"modes": dict(self._on_disk)}


def _store(data, on_disk=None):
    """The real object with its Store replaced.

    `_async_load_locked` returns `self._data` whenever it is already set, so setting it
    directly runs the real methods without Home Assistant storage. `_new_store` is
    replaced too, because `async_remove` verifies the write by reading back through a
    fresh Store: `on_disk` is what that read comes back with, defaulting to whatever the
    save just wrote.
    """
    store = object.__new__(IlluminatorRestoreStore)
    store._lock = asyncio.Lock()
    store._data = dict(data)
    backing = _Store(data if on_disk is None else on_disk)
    store._store = backing
    store._new_store = lambda: backing
    return store


# --- reading back the keys ----------------------------------------------------

async def test_the_keys_come_back_as_channel_and_profile_numbers():
    """They are stored as "channel:profile" strings and used as a pair of ints."""
    store = _store({"0:1": "Auto", "3:2": "Off"})

    assert sorted(await store.async_keys()) == [(0, 1), (3, 2)]


async def test_nothing_outstanding_is_an_empty_list():
    assert await _store({}).async_keys() == []


async def test_a_key_that_is_not_two_numbers_is_skipped_not_raised():
    """This is read at startup to decide what to put back. A single unparseable key from
    an older layout, or from a partial write, must not take the whole recovery with it:
    the other channels still have a camera setting to restore."""
    store = _store({"0:1": "Auto", "nonsense": "Off", "2:3": "Auto"})

    assert sorted(await store.async_keys()) == [(0, 1), (2, 3)]


async def test_a_key_with_too_many_parts_is_skipped_too():
    """`int(part) for part in key.split(":")` unpacks into exactly two names, so three
    parts raise ValueError rather than being silently truncated."""
    store = _store({"0:1:2": "Auto", "4:5": "Off"})

    assert await store.async_keys() == [(4, 5)]


async def test_a_key_whose_parts_are_not_numbers_is_skipped():
    store = _store({"a:b": "Auto", "6:7": "Off"})

    assert await store.async_keys() == [(6, 7)]


# --- removing one ------------------------------------------------------------

async def test_removing_a_key_writes_the_rest():
    store = _store({"0:1": "Auto", "2:3": "Off"})

    await store.async_remove(0, 1)

    assert store._store.saved == [{"modes": {"2:3": "Off"}}]


async def test_removing_something_that_is_not_there_writes_nothing():
    """It returns before saving. Writing an unchanged copy would be a pointless atomic
    replacement of the one file that must not be lost, on every poll that found nothing
    to restore."""
    store = _store({"2:3": "Off"})

    await store.async_remove(0, 1)

    assert store._store.saved == [], "rewrote the file with no change to make"
    assert store._data == {"2:3": "Off"}


async def test_removing_one_does_not_disturb_the_other_channels():
    """A recorder has one of these per channel and profile, and they are restored
    independently."""
    store = _store({"0:1": "Auto", "0:2": "Off", "1:1": "Auto"})

    await store.async_remove(0, 2)

    assert store._store.saved == [{"modes": {"0:1": "Auto", "1:1": "Auto"}}]


async def test_the_removal_is_verified_against_what_landed_on_disk():
    """`async_remove` does not trust the save. It reads back through a fresh Store and
    raises if the key is still there, because this is the only copy of the camera's
    pre-takeover setting and a write that silently did not land would lose it while
    looking like it had been dealt with."""
    store = _store({"0:1": "Auto"}, on_disk={"0:1": "Auto"})

    async def _save_nothing(data):
        store._store.saved.append(data)      # recorded, but never reaches the disk

    store._store.async_save = _save_nothing

    with pytest.raises(RuntimeError):
        await store.async_remove(0, 1)


async def test_a_verified_removal_updates_what_is_held_in_memory():
    """The in-memory copy is what later reads use, so it has to follow the disk rather
    than be left stale until something reloads."""
    store = _store({"0:1": "Auto", "2:3": "Off"})

    await store.async_remove(0, 1)

    assert store._data == {"2:3": "Off"}
