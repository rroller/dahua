"""One file per config entry, and every channel of a recorder writes to it.

`coordinator.py` builds both of these stores in its constructor, and a
recorder has one coordinator per channel (#827), so twelve channels of one
entry hold twelve store objects over one file. Each kept its own cache of
that file's contents and its own lock, which means the last channel to write
wrote its own stale copy over everything another channel had saved.

The cache is filled early and never refreshed: the poll's
`async_reconcile_lighting_scheme_restore_modes` calls `async_keys()` on every
channel, and startup recovery calls `async_channels()`. So by the time anyone
turns an illuminator on, every channel is holding a copy of the file as it
looked before.

What that costs is the thing these stores exist to prevent. The camera's
WhiteMode write is persistent and this is the only record of what was there
before it, so a channel whose record is erased is a camera left in a mode
nobody chose, with nothing left that knows what to put back. In memory it
looks fine until the next restart, which is when the file is read again.
"""

import asyncio

import pytest

from custom_components.dahua.illuminator_restore import (
    ChannelLightingSnapshotStore,
    IlluminatorRestoreStore,
)

ENTRY = "entry-with-twelve-channels"


def _snapshot(scheme):
    return {"LightingScheme": [{"LightingMode": scheme}]}


# --- the snapshots a channel-scoped override holds --------------------------


async def test_two_channels_keep_each_others_snapshots(hass):
    """Both caches are primed first, the way the poll primes them."""
    channel_2 = ChannelLightingSnapshotStore(hass, ENTRY)
    channel_5 = ChannelLightingSnapshotStore(hass, ENTRY)
    assert await channel_2.async_channels() == []
    assert await channel_5.async_channels() == []

    await channel_2.async_set(2, _snapshot("AIMode"))
    await channel_5.async_set(5, _snapshot("Auto"))

    after_restart = ChannelLightingSnapshotStore(hass, ENTRY)
    assert sorted(await after_restart.async_channels()) == [2, 5], (
        "a channel's only way back out of WhiteMode was overwritten by "
        "another channel saving its own"
    )


async def test_a_channel_sees_what_another_channel_just_saved(hass):
    """Not only on disk. Recovery asks the store it has, not a fresh one."""
    channel_2 = ChannelLightingSnapshotStore(hass, ENTRY)
    channel_5 = ChannelLightingSnapshotStore(hass, ENTRY)
    await channel_5.async_channels()

    await channel_2.async_set(2, _snapshot("AIMode"))

    assert await channel_5.async_get(2) == _snapshot("AIMode")


async def test_removing_one_channels_snapshot_leaves_the_others(hass):
    """The undo path removes a snapshot once the camera is back. Doing that
    through one channel's store must not take the rest with it."""
    channel_2 = ChannelLightingSnapshotStore(hass, ENTRY)
    channel_5 = ChannelLightingSnapshotStore(hass, ENTRY)
    await channel_2.async_set(2, _snapshot("AIMode"))
    await channel_5.async_set(5, _snapshot("Auto"))

    await channel_2.async_remove(2)

    after_restart = ChannelLightingSnapshotStore(hass, ENTRY)
    assert await after_restart.async_channels() == [5]


async def test_two_channels_turning_on_at_once_both_survive(hass):
    """A lock per object is not a lock, when the thing being guarded is one
    file. Two channels of one recorder are told to turn on together by a
    scene or a script, which is the case with no serialisation at all."""
    channel_2 = ChannelLightingSnapshotStore(hass, ENTRY)
    channel_5 = ChannelLightingSnapshotStore(hass, ENTRY)

    await asyncio.gather(
        channel_2.async_set(2, _snapshot("AIMode")),
        channel_5.async_set(5, _snapshot("Auto")),
    )

    after_restart = ChannelLightingSnapshotStore(hass, ENTRY)
    assert sorted(await after_restart.async_channels()) == [2, 5]


# --- and the lighting modes the profile override holds ----------------------


async def test_two_channels_keep_each_others_restore_modes(hass):
    """The same shape, and the same constructor, one file along."""
    channel_2 = IlluminatorRestoreStore(hass, ENTRY)
    channel_5 = IlluminatorRestoreStore(hass, ENTRY)
    assert await channel_2.async_keys() == []
    assert await channel_5.async_keys() == []

    await channel_2.async_set(2, 0, "AIMode")
    await channel_5.async_set(5, 0, "Auto")

    after_restart = IlluminatorRestoreStore(hass, ENTRY)
    assert sorted(await after_restart.async_keys()) == [(2, 0), (5, 0)]


async def test_a_reconcile_that_discards_one_key_keeps_the_rest(hass):
    """`async_reconcile_lighting_scheme_restore_modes` drops a key once the
    camera is confirmed out of WhiteMode, once per channel per poll. With a
    cache each, the drop rewrites the file from whatever that channel last
    saw."""
    channel_2 = IlluminatorRestoreStore(hass, ENTRY)
    channel_5 = IlluminatorRestoreStore(hass, ENTRY)
    await channel_2.async_set(2, 0, "AIMode")
    await channel_5.async_set(5, 0, "Auto")

    await channel_5.async_remove(5, 0)

    after_restart = IlluminatorRestoreStore(hass, ENTRY)
    assert await after_restart.async_keys() == [(2, 0)]


# --- what must stay apart ---------------------------------------------------


async def test_two_entries_do_not_share_anything(hass):
    """One file per entry is the whole scheme. A doorbell and a recorder on
    one Home Assistant are two files and must stay that way."""
    recorder = ChannelLightingSnapshotStore(hass, "recorder")
    doorbell = ChannelLightingSnapshotStore(hass, "doorbell")

    await recorder.async_set(2, _snapshot("AIMode"))

    assert await doorbell.async_channels() == []
    assert await doorbell.async_get(2) is None


async def test_removing_the_entrys_file_is_seen_by_every_channel(hass):
    """Removing the entry deletes the file. A channel still holding the old
    contents would hand back a snapshot for a camera the user has removed."""
    channel_2 = ChannelLightingSnapshotStore(hass, ENTRY)
    channel_5 = ChannelLightingSnapshotStore(hass, ENTRY)
    await channel_2.async_set(2, _snapshot("AIMode"))
    await channel_5.async_channels()

    await channel_2.async_remove_all()

    assert await channel_5.async_channels() == []
    assert await channel_5.async_get(2) is None
