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

import logging

from homeassistant.core import callback
from homeassistant.helpers.dispatcher import (
    async_dispatcher_connect,
    async_dispatcher_send,
)

from .const import DOMAIN

# Late rebuilds in flight, per entry id. In `hass.data` rather than a module
# global so a reload cannot inherit a count from the run before it.
DATA_LATE_REBUILDS = "{0}_late_rebuilds".format(DOMAIN)

_LOGGER = logging.getLogger(__package__)


def capability_discovered_signal(entry_id: str) -> str:
    """The signal a coordinator sends when a device finally answers.

    One signal per entry, carrying the channel that answered. A platform
    listens once for the whole entry and rebuilds only the channel named, which
    keeps a recorder's nineteen channels from being rebuilt because one of them
    finally answered.
    """
    return "{0}_capability_discovered_{1}".format(DOMAIN, entry_id)


def late_work_settled_signal(entry_id: str) -> str:
    """The signal for "nothing this entry was waiting on is outstanding now".

    Separate from the discovery signal above, which says a capability turned
    up and starts work. This one says the work finished, and it is what the
    leftover-rows card listens for: that card may only be offered once every
    entity that is still coming has arrived, and a late rebuild is exactly
    the case where one is (@rroller on #1125).
    """
    return "{0}_late_work_settled_{1}".format(DOMAIN, entry_id)


def async_notify_late_work_settled(hass, entry_id: str) -> None:
    """Tell whoever was waiting that the late work for this entry is done."""
    async_dispatcher_send(hass, late_work_settled_signal(entry_id))


def async_rebuilds_outstanding(hass, entry_id: str) -> int:
    """How much late work for this entry has started and not finished.

    ⚠ Read by the leftover-rows health gate, and it counts two things: the
    rebuild tasks themselves, and every entity handed to `async_add_entities`
    whose registration Home Assistant has neither finished nor aborted.

    The second half is @rroller's on #1125, and the first attempt here had
    only the first. A capability that answers late leaves
    `probes_that_did_not_answer` the instant the probe returns, and the
    callback a platform hands us is `_async_schedule_add_entities_for_entry`,
    which SCHEDULES the add and returns. Once setup is complete the
    EntityPlatform tracks that task nowhere this code can await, so counting
    only the rebuild said "settled" while the rows were still empty. He
    reproduced the card offering a row whose registration he had paused.

    So the release is `add_to_platform_finish` or `add_to_platform_abort`,
    which is Home Assistant telling us the answer rather than us guessing how
    long it takes. His second point was that guessing cannot work:
    `DahuaIlluminator.async_added_to_hass` awaits startup restoration, device
    reads included, so an add is not bounded by any number anyone picks.
    """
    held = _held(hass)
    return 0 if held is None else held.get(entry_id, {}).get("work", 0)


def async_rebuilds_failed(hass, entry_id: str) -> bool:
    """Whether a late rebuild for this entry failed and has not since worked.

    ⭐ Also @rroller on #1125: "Track failed rebuilds and keep their rows
    ineligible until a successful rebuild or reload." A build that raised did
    not create its entities, so their rows have no entity behind them for a
    reason that is a fault here and not evidence of anything. Offering them
    for deletion is the same misclassification the platform-setup guard above
    exists to prevent; the first version of this file did exactly that, and
    had a test asserting it.

    Cleared per channel when that channel rebuilds without raising, and with
    the whole record when the entry unloads.
    """
    held = _held(hass)
    return bool(held and held.get(entry_id, {}).get("failed"))


def async_forget_entry(hass, entry_id: str) -> None:
    """Drop everything held for an entry, which unload must do.

    Without it a channel whose rebuild failed before a reload would keep the
    card shut after it, on evidence from a run that no longer exists.
    """
    held = _held(hass)
    if held is not None:
        held.pop(entry_id, None)


def _held(hass):
    """This run's late-work record, or None where there is nowhere to keep it.

    ⚠ None rather than a raise. Plenty of tests here hand the platforms a
    stand-in for `hass` carrying only what the behaviour under test touches,
    and several of those drive a late rebuild. This is bookkeeping for a
    tidy-up card, so a double that cannot hold it simply does not, and the
    gate reads "nothing outstanding" rather than erroring.
    """
    data = getattr(hass, "data", None)
    if data is None:
        return None
    held = data.get(DATA_LATE_REBUILDS)
    if held is None:
        held = data[DATA_LATE_REBUILDS] = {}
    return held


def _for_entry(hass, entry_id: str):
    held = _held(hass)
    if held is None:
        return None
    return held.setdefault(entry_id, {"work": 0, "failed": set()})


def _note_failed(
    hass, entry_id: str, builder, channel_key, failed: bool, kind="build"
) -> None:
    """Remember, or forget, that one builder could not finish one channel.

    ⚠ Keyed by BUILDER as well as channel, @rroller on #1125. Every call to
    `async_add_and_keep_watching` subscribes to the same discovery signal, so
    ten platforms rebuild the same channel from ten different `build`
    functions. Keyed by channel alone, the first of them to succeed cleared a
    failure that belonged to another, and he reproduced the valid row being
    offered for deletion straight afterwards. A builder may only speak for
    its own work.

    ⚠ And by KIND, for the same reason one step down. A build that returns an
    entity has succeeded at building; whether Home Assistant then managed to
    register that entity is a different question with a different answer, and
    the two were clearing each other.
    """
    record = _for_entry(hass, entry_id)
    if record is None:
        return
    if failed:
        # The object itself, not its id: it lives as long as the entry
        # holds the listener that closes over it, and an id could be
        # reused by something else once it did not.
        record["failed"].add((builder, channel_key, kind))
    else:
        record["failed"].discard((builder, channel_key, kind))


def _change_outstanding(hass, entry_id: str, delta: int):
    record = _for_entry(hass, entry_id)
    if record is None:
        return None
    record["work"] = left = max(record["work"] + delta, 0)
    if not left and not record["failed"]:
        (_held(hass) or {}).pop(entry_id, None)
    return left


def _release(hass, entry_id: str) -> None:
    """Give one unit of late work back, and say so if it was the last."""
    if _change_outstanding(hass, entry_id, -1) == 0:
        async_notify_late_work_settled(hass, entry_id)


def _registration_was_skipped_on_purpose(hass, entity) -> bool:
    """Whether an aborted add was Home Assistant declining rather than failing.

    ⚠ @rroller on #1125: "Registration aborts caused by errors need the same
    distinction from intentional disabled/duplicate skips." `add_to_platform_abort`
    is the same call either way, so the cause is not available; what is
    available is the outcome, which is the thing the card actually cares
    about.

    Two intentional skips, and neither leaves a row that could be deleted by
    mistake:

    - the registry row is **disabled**, which `leftover_rows` excludes
      outright, because there is no entity behind it and the user said so;
    - the entity id **already exists** with a live state, so something else is
      behind the row and it does not read as a leftover either.

    Anything else that aborted left a registry row with nothing behind it for
    a reason that belongs to this integration, which is the reading the gate
    exists to refuse.
    """
    row = getattr(entity, "registry_entry", None)
    if row is not None and getattr(row, "disabled", False):
        return True
    entity_id = getattr(entity, "entity_id", None)
    states = getattr(hass, "states", None)
    if not entity_id or states is None:
        return False
    state = states.get(entity_id)
    # A `restored: True` state is the registry writing the row out as
    # unavailable, which is exactly a leftover and not somebody else's entity.
    return state is not None and not state.attributes.get("restored")


def _hold_until_registered(hass, entry_id: str, builder, channel_key, entities) -> None:
    """Count each entity until Home Assistant finishes or aborts adding it.

    ⚠ Wrapping the entity's own two lifecycle hooks, because they are the only
    honest signal available: `async_add_entities` returns once the add is
    SCHEDULED, and after setup the task it schedules is tracked nowhere this
    code can reach. `add_to_platform_start` always runs first and then exactly
    one of these two, so one release per entity.

    ⚠ No timeout, deliberately. If an add never reports, the entity does not
    exist, and withholding a card that offers to delete its row is the right
    answer rather than a failure mode. A reload clears the record.

    ⚠ A registration that RAISES is recorded, not merely released. That error
    happens inside the add task Home Assistant scheduled, so the build wrapper
    never sees it: @rroller reproduced an entity whose `async_added_to_hass`
    raises, and the row stayed restored while the count went to zero and the
    scan offered it.

    Tolerated rather than demanded: a great many tests here stand in a marker
    object for an entity, and one without these hooks is simply not counted.
    """
    for entity in entities:
        finish = getattr(entity, "add_to_platform_finish", None)
        abort = getattr(entity, "add_to_platform_abort", None)
        if finish is None or abort is None:
            continue
        if _change_outstanding(hass, entry_id, 1) is None:
            return

        async def _finished(_finish=finish) -> None:
            try:
                await _finish()
            except Exception:
                # Raised inside Home Assistant's own add task. Re-raised so HA
                # still reports it, and remembered so the row this entity was
                # going to fill is not offered for deletion in the meantime.
                _note_failed(hass, entry_id, builder, channel_key, True, "registration")
                raise
            else:
                _note_failed(
                    hass, entry_id, builder, channel_key, False, "registration"
                )
            finally:
                _release(hass, entry_id)

        @callback
        def _aborted(_abort=abort, _entity=entity) -> None:
            try:
                _abort()
            finally:
                if not _registration_was_skipped_on_purpose(hass, _entity):
                    _note_failed(
                        hass, entry_id, builder, channel_key, True, "registration"
                    )
                _release(hass, entry_id)

        try:
            entity.add_to_platform_finish = _finished
            entity.add_to_platform_abort = _aborted
        except AttributeError:
            # An entity that will not take an instance attribute, which none
            # here is, but it is cheap to survive. Leaving it counted would
            # hold the gate shut for ever: nothing would ever release it.
            _release(hass, entry_id)


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
    # Who this call is. Every platform makes one of these and they all listen
    # to the same signal, so a failure has to say which builder it belongs to
    # or one platform's success speaks for another's failure (#1125).
    builder = object()

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
                # ⚠ Counted BEFORE the call. Home Assistant starts the add
                # task eagerly, so by the time this returns the entity may
                # already have been registered and released.
                # ⚠ The rebuild's own `channel_key`, which is what the
                # signal named, rather than asking the coordinator again.
                # Several coordinator doubles here cannot answer that, and a
                # real one built with `object.__new__` raises; the key only
                # has to match the one the surrounding rebuild will clear.
                _hold_until_registered(
                    hass, entry.entry_id, builder, channel_key, entities
                )
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

        Counted, because that task is the window in which a capability reads
        as resolved and its entities do not exist yet. Incremented here, in
        the callback, rather than inside the task: the count has to be up
        before this function returns, or the coordinator's own "the waiting is
        over" signal, sent moments later on the same tick, finds nothing
        outstanding and offers the card too early.
        """
        _change_outstanding(hass, entry.entry_id, 1)
        hass.async_create_task(_rebuild_then_settle(channel_key))

    async def _rebuild_then_settle(channel_key: str) -> None:
        try:
            await _add_whatever_is_missing(channel_key)
        except Exception:  # pylint: disable=broad-except
            # Reported here rather than left to escape. This is a background
            # task, so an escaping error was only ever an "exception was never
            # retrieved" line naming neither entry nor channel.
            #
            # ⭐ And remembered. The entities this build owed were not
            # created, so their rows are empty because of a fault here. @kmich
            # on #1114 and @rroller on #1125 are the same point from two
            # sides: a row with nothing behind it is only a leftover when the
            # integration is not itself the reason.
            _note_failed(hass, entry.entry_id, builder, channel_key, True)
            _LOGGER.exception(
                "Could not build the entities %s answered for late", channel_key
            )
        else:
            _note_failed(hass, entry.entry_id, builder, channel_key, False)
        finally:
            # `finally`, so a build that raises cannot leave the count up and
            # the card shut for the life of the entry.
            # `== 0`, not falsy: None means this `hass` keeps no record, so
            # nothing was ever waiting on it and there is nobody to tell.
            _release(hass, entry.entry_id)

    entry.async_on_unload(
        async_dispatcher_connect(
            hass, capability_discovered_signal(entry.entry_id), _on_discovery
        )
    )
