"""Entities the device was not ready to be asked about at setup.

A capability probe that times out tells us nothing, and the integration wrote it
down as "this device does not have that". On a nineteen channel recorder that
cost real entities: #1043 reported two identical IP8M-2493E cameras on one
recorder, one with an Infrared Mode select and a working Infrared light, the
other with neither, decided by which probe happened to expire. Reloading did not
help, because a reload re-probes every channel at once and hits the same wall.

Measured on that install, the set of probes that failed was different on every
run, which is what rules out the comfortable explanation. It is contention, not
hardware, so the answer exists and we simply did not wait for it.

Retrying inside setup was the obvious fix and is the wrong one: each silent probe
would add its full timeout to a startup that reporter had already called out at
four minutes. So the probes are retried later instead, when the device is idle
and the same reads are fast (measured at 0.06s against 20s of timeout), and this
module is what lets an answer that arrives late still produce its entities.

The shape, for a platform adopting it:

    async def async_setup_entry(hass, entry, async_add_entities):
        async def build(coordinator):
            return [SomeEntity(coordinator, entry)] if coordinator.supports_x() else []

        await async_add_and_keep_watching(hass, entry, build, async_add_entities)

`build` is called once per coordinator now, and again whenever a capability turns
up late. It must be safe to call repeatedly: entities whose `unique_id` already
exists are dropped rather than added twice.
"""

from homeassistant.core import callback
from homeassistant.helpers.dispatcher import (
    async_dispatcher_connect,
    async_dispatcher_send,
)

from .const import DOMAIN


def capability_discovered_signal(entry_id: str) -> str:
    """The signal a coordinator sends when a device finally answers.

    One signal per entry, carrying the channel that answered. A platform
    listens once for the whole entry and rebuilds only the channel named, which
    keeps a recorder's nineteen channels from being rebuilt because one of them
    finally answered.
    """
    return "{0}_capability_discovered_{1}".format(DOMAIN, entry_id)


def async_notify_capability_discovered(hass, entry_id: str, channel_key: str) -> None:
    """Tell the platforms that a channel can do something it would not admit to.

    `channel_key` is the coordinator's serial number, which carries the channel
    suffix and so names one channel of one device. Rebuilding only that channel
    matters more than it looks: a platform's build may read from the device,
    and select.py enumerates PTZ presets, so an entry wide rebuild would make
    one channel's late answer cost a read on all nineteen.
    """
    async_dispatcher_send(hass, capability_discovered_signal(entry_id), channel_key)


def _key(entity):
    """What identifies an entity for the purpose of not creating it twice.

    `getattr` rather than `entity.unique_id`, and None meaning "add it", for
    two different reasons. A Home Assistant entity may legitimately carry no
    unique_id, and two of those are not the same entity, so deduplicating them
    against each other would drop one. And a great many tests here replace an
    entity class with a marker to record that setup decided to create it; that
    is a reasonable thing for a test to do, and a mechanism that forbids it is
    a mechanism nobody will adopt for the other dozen capabilities.

    Everything this integration creates defines a unique_id, so the real path
    is always the deduplicated one.
    """
    return getattr(entity, "unique_id", None)


async def async_add_and_keep_watching(
    hass, entry, build, async_add_entities, coordinators, subentry_for=None
) -> None:
    """Add what each coordinator warrants, now and whenever that changes.

    `created` is local to this call rather than stored on `hass.data`, which
    matters on reload: a reload runs setup again, and a set that outlived the
    previous run would recognise every entity as already created and add
    nothing at all.

    Entities that arrive late are filed under the same `config_subentry_id` as
    the ones that arrived on time, so a channel's entities stay with its
    channel (#827) instead of landing on the entry.

    `coordinators` is the platform's own `entry_coordinators`, handed in rather
    than imported here. Importing it would be a cycle, since the package imports
    the platforms, and resolving it late would quietly bypass the platforms'
    tests, several of which replace that name to choose what setup sees.

    `subentry_for(entity, coordinator)` overrides that filing for a platform
    that creates both kinds in one build. A recorder's processor and memory
    sensors describe the box rather than the channel that carries them, and
    they are filed on the entry; the channel's own sensors beside them are
    not. Without this the two could not be built together, which is what kept
    the load sensors out of the rebuild and so out of #1057 entirely.
    """
    created: set = set()

    async def _add_whatever_is_missing(channel_key=None) -> None:
        for coordinator in coordinators(entry).values():
            if (
                channel_key is not None
                and coordinator.get_serial_number() != channel_key
            ):
                continue
            fresh = [
                entity
                for entity in await build(coordinator)
                if _key(entity) is None or _key(entity) not in created
            ]
            if not fresh:
                continue
            created.update(key for key in map(_key, fresh) if key is not None)
            # One call per destination, which is one call in all but the
            # recorder case above.
            filed: dict = {}
            for entity in fresh:
                where = (
                    subentry_for(entity, coordinator)
                    if subentry_for is not None
                    else coordinator.subentry_id
                )
                filed.setdefault(where, []).append(entity)
            for where, entities in filed.items():
                async_add_entities(entities, config_subentry_id=where)

    await _add_whatever_is_missing()

    @callback
    def _on_discovery(channel_key: str) -> None:
        """Start the rebuild. The decorator is the whole of #1043's second report.

        Home Assistant decides where to run a dispatcher target by looking at
        the target: a coroutine function becomes a task, a `@callback` runs on
        the event loop, and anything else is treated as blocking and handed to
        a worker thread. Without the decorator this ran in `SyncWorker_N`, and
        `hass.async_create_task` refuses to be called from there, so every late
        answer on that install raised and created nothing.

        A task rather than awaiting here, because the retry runs at the top of
        a poll: the rebuild must look at `data` after the poll has written it.
        """
        hass.async_create_task(_add_whatever_is_missing(channel_key))

    entry.async_on_unload(
        async_dispatcher_connect(
            hass, capability_discovered_signal(entry.entry_id), _on_discovery
        )
    )
