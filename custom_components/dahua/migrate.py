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

import asyncio
import logging
import os
import shutil
import tempfile
import time

from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import entity_platform as ep
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.event import async_call_later

from . import ISSUE_LEFTOVER_ROWS, ISSUE_SIBLINGS_REMAIN, devices_with_identifier
from .const import (
    CHANNEL_OPTION_KEYS,
    CONF_ADDRESS,
    CONF_CHANNEL,
    CONF_PORT,
    DOMAIN,
    PLATFORMS,
)
from .host import is_synthesised_identity, normalize_address

_LOGGER: logging.Logger = logging.getLogger(__package__)

# How long after the last thing the leftover gate was waiting on before the
# registry is scanned again. See the docstring on the recheck below: an
# entity whose add has been scheduled does not exist yet, and the scan does
# not await, so it would otherwise read the row as a leftover.
LEFTOVER_RECHECK_SETTLE_SECONDS = 5

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


def _channel_settings(entry) -> dict:
    """What this channel is actually configured with, not what it was added with.

    A subentry's data is where a merged recorder keeps one channel's own answers,
    and `channel_option` reads it before the entry's options. Copying only
    `entry.data` therefore did not just fail to carry the user's later changes: it
    made the add-time value *shadow* them, permanently, for every key stored in
    both. A channel whose events were changed from VideoMotion to SmartMotionHuman
    in Configure went back to VideoMotion at migration and could not be corrected
    through the entry's options again, and an empty selection was lost the same way.

    Only the per-channel keys are overlaid. A host-wide option left here would be
    frozen onto each channel instead of continuing to follow the host.

    Reported by alpha520098 on #825, who also identified the key set.
    """
    settings = dict(entry.data)
    settings.update(
        {
            key: value
            for key, value in (entry.options or {}).items()
            if key in CHANNEL_OPTION_KEYS
        }
    )
    return settings


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


def _rows_span_multiple_subentries(rows) -> bool:
    """Whether these registry rows sit across more than one subentry."""
    seen = set()
    for row in rows:
        subentry_id = getattr(row, "config_subentry_id", None)
        if subentry_id is not None:
            seen.add(subentry_id)
            if len(seen) > 1:
                return True
    return False


def _has_finished_merging(entities, devices, entry) -> bool:
    """Whether this entry is a recorder that has already *finished* merging.

    This is the signal that separates a finished merge from one still in progress,
    and getting it right is the whole of #972. Both have subentries, because they
    are all created before any row moves, so the presence of subentries alone
    cannot tell them apart. What differs is where the rows sit. A finished recorder
    has them spread across a subentry per channel. A partial merge has them still
    on the flat entries it has not folded in yet, so the entry that carries the
    subentries holds rows under at most one of them.

    Re-grouping a finished recorder is the bug: the merge reads it as one channel
    and moves every channel's rows onto a single subentry. A partial merge, by
    contrast, must be re-entered and finished, so it must *not* match here.

    Both registries are checked: the entity spread catches every real recorder
    (a working channel has entities), and the device spread closes the gap where a
    channel registered a device but no entity. `config_subentry_id` is the field
    on both a RegistryEntry and a DeviceEntry; read defensively so a registry that
    does not carry it simply contributes nothing rather than raising.
    """
    return _rows_span_multiple_subentries(
        er.async_entries_for_config_entry(entities, entry.entry_id)
    ) or _rows_span_multiple_subentries(
        dr.async_entries_for_config_entry(devices, entry.entry_id)
    )


def _backup(hass: HomeAssistant, label: str = "pre-merge") -> str | None:
    """Copy the registries somewhere the user can find them. Blocking."""
    storage = hass.config.path(".storage")
    target = hass.config.path(
        "dahua-%s-backup-%s" % (label, time.strftime("%Y%m%d-%H%M%S"))
    )
    copied = []
    try:
        if label == "pre-serial":
            # Config entries contain credentials. Use a private, unique
            # directory, including when two entries migrate in the same second.
            target = tempfile.mkdtemp(
                prefix="dahua-pre-serial-backup-", dir=hass.config.path()
            )
        else:
            os.makedirs(target, exist_ok=True)
        for name in BACKED_UP:
            source = os.path.join(storage, name)
            if os.path.exists(source):
                shutil.copy2(source, os.path.join(target, name))
                copied.append(name)
    except OSError:
        _LOGGER.exception(
            "Could not back up the Home Assistant registries before merging "
            "Dahua entries. The merge has NOT been attempted"
        )
        return None
    if not copied or (label == "pre-serial" and set(copied) != set(BACKED_UP)):
        return None
    return target


DATA_SERIAL_BACKUP = "dahua_pre_serial_backup"


def _registry_fingerprint(hass: HomeAssistant, directory: str | None = None) -> tuple:
    """What the files a backup would copy look like right now. Blocking."""
    storage = directory if directory is not None else hass.config.path(".storage")
    marks = []
    for name in sorted(BACKED_UP):
        try:
            stat = os.stat(os.path.join(storage, name))
        except OSError:
            marks.append((name, None, None))
        else:
            marks.append((name, stat.st_size, stat.st_mtime_ns))
    return tuple(marks)


async def _async_backup_once(hass: HomeAssistant) -> str | None:
    """One copy of the registries, shared by every entry it still describes.

    Reported by @kmich on three monitors migrating in one startup: three backup
    directories written within 0.1 s of each other and byte identical. A backup
    copies the *persisted* files, and Home Assistant saves those on a delay, so
    nothing any of the three migrations did had reached disk yet.

    Reused on the evidence rather than on a timer. The files are stat'd, and the
    copy already taken is handed back only while their sizes and modification
    times are what they were when it was taken. That is the whole safety
    property a backup has -- it is the state immediately before the change --
    and a window in seconds would have traded it for a round number. An entry
    reloaded an hour later, after Home Assistant has persisted anything at all,
    gets its own copy.

    Behind a lock, because the case being fixed is entries setting up
    concurrently: without it all three would stat before any of them copied,
    see nothing, and write the three directories again.

    A failed backup is not remembered, so the next entry tries rather than
    inheriting a full disk that may since have been emptied. The caller still
    refuses to migrate on None.
    """
    held = hass.data.setdefault(
        DATA_SERIAL_BACKUP, {"lock": asyncio.Lock(), "fingerprint": None, "where": None}
    )
    async with held["lock"]:
        fingerprint = await hass.async_add_executor_job(_registry_fingerprint, hass)
        if held["where"] is not None and held["fingerprint"] == fingerprint:
            copied = await hass.async_add_executor_job(
                _registry_fingerprint, hass, held["where"]
            )
            if copied == fingerprint and all(size is not None for _, size, _ in copied):
                _LOGGER.debug(
                    "Reusing the complete registry backup at %s", held["where"]
                )
                return held["where"]
        where = await hass.async_add_executor_job(_backup, hass, "pre-serial")
        if where is not None:
            held["fingerprint"] = fingerprint
            held["where"] = where
        return where


def leftover_rows(hass: HomeAssistant, entry) -> list:
    """This entry's registry rows that no entity is behind, newest state first.

    A row whose entity the integration no longer creates stays in the registry
    for ever, unavailable, and Home Assistant offers to delete it one at a
    time. @kmich hit a monitor's worth of them at once: the `disarming` and
    `event_notifications` switches an earlier release created on a VTH are
    gated off now, so they moved through the serial migration and then sat
    there with no way to be rid of them but by hand.

    **A row with no live entity is one with no state object, or one whose state
    Home Assistant marked `restored`.** Both, because which of the two it is
    depends on timing rather than on anything meaningful: `_write_unavailable_states`
    runs on EVENT_HOMEASSISTANT_START and only when Home Assistant was not
    already running, so before it a leftover has no state at all and after it
    has a restored one. Reading only `restored` would have made the answer
    depend on listener order.

    A disabled row is never a leftover. There is no entity behind it because
    the user said so.
    """
    registry = er.async_get(hass)
    rows = []
    for row in er.async_entries_for_config_entry(registry, entry.entry_id):
        if row.disabled_by is not None:
            continue
        if not entry.options.get(row.domain, True):
            continue
        state = hass.states.get(row.entity_id)
        if state is None or state.attributes.get("restored"):
            rows.append(row.entity_id)
    return sorted(rows)


def _entry_is_wholly_healthy(hass: HomeAssistant, entry, coordinators) -> bool:
    """Whether this entry is in a state where a missing entity means anything.

    The hazard this exists for is #1085's channel recovery. A channel that
    failed its first refresh has every one of its rows unavailable until it
    comes back, and a card offering to delete all of them is how the #827
    merge card destroyed 232 entities. The same goes for a capability that has
    not answered yet (#1057): its entity is built when the retry succeeds, and
    until then the row looks exactly like a leftover.

    So this says yes only when every channel is initialized and polling
    cleanly. Anything else and nothing is offered, which costs a user who
    really does have leftovers one more restart and costs a user whose camera
    is merely offline nothing at all.
    """
    if not coordinators:
        return False
    from . import channel_configs

    expected = {config.get(CONF_CHANNEL, 0) for _, config in channel_configs(entry)}
    present = {
        coordinator.get_channel()
        for coordinator in coordinators
        if callable(getattr(coordinator, "get_channel", None))
    }
    # Setup retains successful coordinators only. Checking that list alone
    # cannot establish that a failed configured channel has recovered.
    if not expected.issubset(present):
        return False
    ready = {
        platform.domain
        for platform in ep.async_get_platforms(hass, DOMAIN)
        if platform.config_entry is entry
        and getattr(platform, "_setup_complete", False)
    }
    wanted = {domain for domain in PLATFORMS if entry.options.get(domain, True)}
    if not wanted.issubset(ready):
        # EntityPlatform handles setup failures internally; a successful
        # coordinator refresh does not prove the entities were loaded.
        return False
    for coordinator in coordinators:
        if not getattr(coordinator, "initialized", False):
            return False
        if not getattr(coordinator, "last_update_success", False):
            return False
        # ⚠ `may_still_answer`, not `did_not_answer`. A probe is worth waiting
        # on only while something will ask it again; one that has spent its
        # retry budget, or that is not retried at all, will never build its
        # entity, so holding the card back for it holds it back for ever.
        # @kmich measured that on #1114: seventeen entries, every one of them
        # reporting `cloud_upgrade` unanswered with its budget spent, and the
        # card could not appear on any of them.
        #
        # The fallback is the older method, so a coordinator double that
        # models only that one keeps the stricter behaviour rather than
        # silently losing the gate.
        pending = getattr(coordinator, "probes_that_may_still_answer", None)
        if pending is None:
            pending = getattr(coordinator, "probes_that_did_not_answer", None)
        if pending is not None and pending():
            # A probe that may still answer is one whose entity is still to
            # come: the retry builds it when the device finally replies, and
            # until then its row is indistinguishable from a leftover.
            return False
    # ⚠ And a probe that HAS answered is not the end of it. The entities it
    # gates are built by `late.py` in a task started after the poll, so for
    # the length of that task the question reads as resolved while the rows
    # are still empty. @rroller on #1125 named the repair flow specifically:
    # it confirms a removal through this same gate.
    from .late import async_rebuilds_failed, async_rebuilds_outstanding

    if async_rebuilds_outstanding(hass, entry.entry_id):
        return False
    # ⚠ And a rebuild that FAILED is not the same as one that found nothing.
    # Its entities were never created, so their rows are empty because of a
    # fault here, which is the one reading this gate exists to refuse. It
    # stays refused until that channel rebuilds cleanly or the entry reloads.
    if async_rebuilds_failed(hass, entry.entry_id):
        return False
    return True


@callback
def async_recheck_leftover_rows_when_late_work_settles(
    hass: HomeAssistant, entry, coordinators
) -> None:
    """Offer the card again whenever something the gate was waiting on resolves.

    ⭐ @rroller on #1125. `async_offer_leftover_rows` is called once, from
    `async_setup_entry` after the platforms are forwarded. At that moment a
    retryable probe that has not answered still has its whole budget, so the
    gate rightly refuses and no card appears. The budget is then spent over
    the next few polls, `probes_that_may_still_answer` empties -- and nothing
    asks again. The card stays absent for ever, which is @kmich's #1114
    reached by a second road, and the first fix on this branch did not close
    it: the test that proved it called the offer a second time by hand.

    So the two places where that waiting ends now say so, and this listens:
    the retry loop when its last question is settled, and `late.py` when a
    rebuild it started has finished creating entities.

    ⚠ **Delayed, and that is not a style choice.** `async_add_entities` only
    SCHEDULES the add, and after setup is complete the EntityPlatform tracks
    that task nowhere this code can await. Home Assistant starts it eagerly,
    so it runs as far as its first await and hands control straight back,
    which means a rebuild can finish, this fire, and the scan read the rows of
    entities that are a few awaits away from existing. The scan itself does
    not await, so it would run to the end before they arrived. A few seconds
    is far longer than an add takes and costs nothing for a tidy-up card.
    Collapsed too, so a recorder settling nineteen channels scans once.

    Wrapped like the setup call site. This runs on the event loop beside a
    poll, and a tidy-up card must never be what stops a camera working.
    """
    due: list = []

    @callback
    def _settled() -> None:
        while due:
            due.pop()()
        due.append(
            async_call_later(hass, LEFTOVER_RECHECK_SETTLE_SECONDS, _recheck_now)
        )

    @callback
    def _recheck_now(_now) -> None:
        due.clear()
        hass.async_create_task(_async_recheck_leftover_rows(hass, entry, coordinators))

    from .late import async_forget_entry, late_work_settled_signal

    entry.async_on_unload(
        async_dispatcher_connect(
            hass, late_work_settled_signal(entry.entry_id), _settled
        )
    )

    @callback
    def _forget_what_was_outstanding() -> None:
        """Unload clears both the pending scan and the late-work record.

        ⚠ Registered here rather than in `late.py`, which runs once per
        PLATFORM: an unload hook added there would be added ten times, and
        two tests count an entry's hooks to check that every platform is
        listening for a late answer.

        The record matters as much as the timer. A channel whose rebuild
        failed before a reload would otherwise keep the card shut after it,
        on evidence from a run that no longer exists.
        """
        while due:
            due.pop()()
        async_forget_entry(hass, entry.entry_id)

    entry.async_on_unload(_forget_what_was_outstanding)


async def _async_recheck_leftover_rows(
    hass: HomeAssistant, entry, coordinators
) -> None:
    # ⚠ Read now, not closed over from setup. A channel that failed its first
    # refresh and recovered later (#1085) is in `runtime_data` and was not in
    # the list setup built, and the gate requires every configured channel to
    # be present before it offers anything.
    live = getattr(entry, "runtime_data", None)
    if isinstance(live, dict) and live:
        coordinators = list(live.values())
    try:
        await async_offer_leftover_rows(hass, entry, coordinators)
    except Exception:  # pylint: disable=broad-except
        _LOGGER.exception(
            "Could not re-check %s for leftover registry rows. Nothing is wrong "
            "with the device and the entry is set up normally",
            entry.data.get(CONF_ADDRESS),
        )


async def async_offer_leftover_rows(hass: HomeAssistant, entry, coordinators) -> int:
    """Raise a card listing this entry's leftover rows, or clear one that is stale.

    Returns how many were found, for the caller's log and for the tests.

    **Recomputed on every start and never persisted.** The merge card that
    deleted a recorder was persistent, so it outlived the evidence it was
    raised on and then described a situation that no longer existed. This one
    is rebuilt from the registry each time, and the flow rebuilds it again
    before it deletes anything.

    A reload is fine, and this used to claim otherwise. Unloading writes every
    row as unavailable with `restored: True` (`Entity.async_remove`, the
    `write_unavailable_state` branch), so after a reload a row that really has
    stopped being created is correctly found. What that also means is that
    every row of an entry which has not finished coming back reads as a
    leftover, which is what the health gate above is for, and what the repair
    flow re-checks before it removes anything. The earlier wording here said a
    reload writes no restored states, which is wrong, and it was the reasoning
    the safety rested on (@kmich on #1114).
    """
    issue_id = ISSUE_LEFTOVER_ROWS.format(entry.entry_id)
    if not _entry_is_wholly_healthy(hass, entry, coordinators):
        ir.async_delete_issue(hass, DOMAIN, issue_id)
        return 0
    rows = leftover_rows(hass, entry)
    if not rows:
        ir.async_delete_issue(hass, DOMAIN, issue_id)
        return 0
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=True,
        is_persistent=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="leftover_rows",
        translation_placeholders={
            "name": entry.title or "untitled",
            "count": str(len(rows)),
        },
        data={"entry_id": entry.entry_id},
    )
    return len(rows)


async def async_drop_duplicate_disk_sensors(hass: HomeAssistant, entry) -> None:
    """Remove the disk sensors every channel of a recorder used to create.

    `storageDevice.cgi?action=getDeviceAllInfo` takes no channel: the disks
    belong to the box. Every channel coordinator answered that question anyway
    and created its own sensor for each disk, so a twelve channel recorder with
    four bays carried forty eight entities describing four disks, each filed
    under a device named after a camera.

    They are created once now, keyed on the device serial. On channel 0 that is
    the same string as before, so the surviving four keep their ids, their
    history and anything pointing at them. These are the rest.

    **The safety property, and the reason this is not a pattern match alone: a
    duplicate is only removed when the surviving id is present.** So if a
    recorder's entry has no channel 0 -- the canonical id would be one nothing
    has created yet -- this leaves every row alone rather than deleting the only
    copy of a disk. Clutter is a better failure than loss, and #891 is why that
    is spelled out.
    """
    registry = er.async_get(hass)
    rows = er.async_entries_for_config_entry(registry, entry.entry_id)
    by_unique_id = {row.unique_id: row for row in rows}

    doomed = []
    for unique_id, row in by_unique_id.items():
        head, separator, slug = (unique_id or "").partition("_disk_")
        if not separator or not slug:
            continue
        # `<serial>_<channel>_disk_<slug>` is a duplicate; `<serial>_disk_<slug>`
        # is the one that stays. rpartition so a serial containing an underscore
        # cannot be mistaken for a channel.
        serial, underscore, channel = head.rpartition("_")
        if not underscore or not channel.isdigit():
            continue
        survivor = "%s_disk_%s" % (serial, slug)
        if survivor != unique_id and survivor in by_unique_id:
            doomed.append(row)

    if not doomed:
        return

    where = await hass.async_add_executor_job(_backup, hass, "pre-disk-cleanup")
    if where is None:
        _LOGGER.warning(
            "Could not back up the registries, so the %d duplicate Dahua disk "
            "sensors have been left alone. They are disabled by default and "
            "harmless; remove them from the entity list if you want them gone",
            len(doomed),
        )
        return

    for row in doomed:
        registry.async_remove(row.entity_id)

    _LOGGER.info(
        "Removed %d duplicate disk sensors: a recorder's channels each created "
        "one per disk, and they are now created once for the device. The ones "
        "that remain keep their entity ids and history. Registries backed up to "
        "%s first",
        len(doomed),
        where,
    )


async def async_merge_channel_entries(hass: HomeAssistant) -> None:
    """Merge every host's per-channel entries into one entry per host.

    Does nothing for a host that already has one entry, which is every single
    camera and every host already merged, so the common case costs one grouping
    pass and no writes at all.
    """
    entries = hass.config_entries.async_entries(DOMAIN)

    by_host: dict[tuple[str, str], list] = {}
    for entry in entries:
        if entry.disabled_by is not None:
            # Left exactly as it is, and not counted towards whether this host
            # needs merging. Disabling an entry is how somebody parks a channel
            # whose camera has gone without throwing its history away, and
            # folding it in would undo that in two different ways.
            #
            # It would be silently re-enabled: its entities would move onto the
            # survivor, which is enabled, so a camera the user switched off starts
            # polling again with nothing said.
            #
            # Worse, the survivor is the lowest channel and nothing here looked at
            # `disabled_by`, so a disabled channel 0 *became* the survivor: every
            # other channel's entities would be moved onto a disabled entry, which
            # Home Assistant never sets up, and the other entries are then removed.
            # A whole recorder goes dark on upgrade, and only the registry copies
            # taken above are the way back.
            continue
        address = _address_of(entry)
        if address:
            by_host.setdefault((address, _port_of(entry)), []).append(entry)

    candidates = {host: group for host, group in by_host.items() if len(group) > 1}
    if not candidates:
        return

    # Only a host with more than one entry can need merging, so the registries
    # are read only now, not on the common no-op pass.
    entities = er.async_get(hass)
    devices = dr.async_get(hass)
    hosts = {}
    for host, group in candidates.items():
        if any(_has_finished_merging(entities, devices, entry) for entry in group):
            # One of these entries has already finished merging: its entities are
            # spread across a subentry per channel. The merge assumes one entry is
            # one channel, so folding this host would move every one of that
            # entry's channels onto a single subentry and discard the rest. No
            # entity is lost, so the count guards would not catch it; only the
            # per-channel structure is destroyed (#972). It arises when a channel
            # disabled at the first merge is later re-enabled, leaving a finished
            # recorder beside a flat entry for that channel. Leave the host as it
            # is -- the entries still work, and this run takes no backup. A merge
            # still in progress is deliberately not caught here: it has subentries
            # too, but has not spread its entities across them yet, so it can be
            # re-entered and finished.
            _LOGGER.debug(
                "Not merging %s: an entry here has already finished merging "
                "(its entities span its subentries) and sits beside %d other "
                "entr(y/ies). Re-merging would collapse its per-channel "
                "structure, so it is left as it is",
                host[0],
                len(group) - 1,
            )
            continue
        hosts[host] = group
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
        len(hosts),
        backup,
    )

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
                address,
                backup,
            )


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
            data=_channel_settings(entry),
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
        for entry in ordered
    )

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
            address,
            expected - on_survivor,
            expected,
        )
        return

    for entry in ordered:
        subentry_id = subentry_for[entry.entry_id]
        for device in list(dr.async_entries_for_config_entry(devices, entry.entry_id)):
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
            address,
            expected,
            survived,
        )
        return

    removed = 0
    removed_hosts = set()
    for entry in ordered[1:]:
        left = er.async_entries_for_config_entry(entities, entry.entry_id)
        if left:
            # Never remove an entry that still owns something: removing it would
            # take those entities and their history with it.
            _LOGGER.error(
                "Not removing the Dahua entry for channel %d of %s: it still owns "
                "%d entities, so the merge is incomplete and removing it would "
                "delete them",
                _channel_of(entry),
                address,
                len(left),
            )
            continue
        await hass.config_entries.async_remove(entry.entry_id)
        removed += 1
        # The address as the removal hook spelled it, not the lowercased group
        # key: normalize_address keeps the case, and the issue id is built from
        # its own normalization. Deleting the lowercased id left a mixed-case
        # host's card standing.
        removed_hosts.add(normalize_address(entry.data.get(CONF_ADDRESS)))

    for removed_host in removed_hosts:
        # These removals are the merge's, not the user's, but each one runs the
        # same removal hook a manual deletion does. That hook sees the surviving
        # entry as a sibling and raises the "more Dahua entries still use
        # <address>" card -- whose fix removes every entry at the address, which
        # is the recorder this merge has just finished creating. Nothing is left
        # to offer, so the card it just raised is withdrawn.
        ir.async_delete_issue(hass, DOMAIN, ISSUE_SIBLINGS_REMAIN.format(removed_host))

    _LOGGER.warning(
        "%s is now one Dahua entry with %d channels: %d entities moved and %d "
        "redundant entries removed, with all %d accounted for on the merged "
        "entry. Entity ids are unchanged, so dashboards and automations keep "
        "working",
        # The subentries the survivor actually has, not the number of entries
        # that were folded into them: two entries can share a channel, and
        # counting entries would then claim a channel that does not exist.
        address,
        len(survivor.subentries),
        moved,
        removed,
        survived,
    )


class SerialConflictPlanChanged(ValueError):
    """The registry moved while the backup was being written.

    Raised instead of removing anything. The card recomputes and asks again,
    so the only cost is a second press, and the alternative is deleting on a
    reason that stopped being true (@rroller, #1146).
    """


class SerialMigrationConflict(ValueError):
    """Two rows want one identity, and no retry changes that.

    Separate from the other `ValueError`s this module raises, which are
    genuinely transient: a registry that moved under us, or a backup that
    could not be taken. `async_setup_entry` retries those and refuses to
    retry this, because "will retry" about a permanent condition is how
    @wiiNinja spent an evening on #1143 watching a card that was never going
    to clear.
    """


def _is_residue(stale, holder) -> bool:
    """Whether the one that cannot move is the one nothing is behind.

    Both halves are required. A disabled row whose identity is wanted by
    another disabled row is two ghosts and says nothing about which is which,
    and an ENABLED row that cannot move is a live entity whose fate is a
    decision rather than a tidy-up. Only disabled-blocked-by-enabled is
    unambiguous, and that is the shape all 44 of @wiiNinja's pairs had.
    """
    return bool(getattr(stale, "disabled_by", None)) and not getattr(
        holder, "disabled_by", None
    )


def _legacy_roots(entry, entry_rows, entry_devices, extra=None) -> set:
    """Every generated identity this entry is filed under.

    The stored identity may predate a password change, so the registries, not
    today's credentials, are authoritative about which legacy IDs need moving.
    `extra` is the hash the client would generate right now, which the
    migration knows and a repair flow run later does not.
    """
    roots = set(extra or ())
    for value in [
        entry.unique_id,
        *[v for d in entry_devices for domain, v in d.identifiers if domain == DOMAIN],
        *[row.unique_id for row in entry_rows if row.platform == DOMAIN],
    ]:
        root = (value or "").split("_", 1)[0]
        if is_synthesised_identity(root):
            roots.add(root)
    return roots


def _replacer(roots, serial):
    """What each identity becomes once `serial` is adopted."""

    def replacement(value):
        for root in roots:
            if value == root or (value or "").startswith(root + "_"):
                return serial + value[len(root) :]
        return value

    return replacement


def _conflict_holders(hass: HomeAssistant, entry, serial):
    """Every blocked row and device, each with whatever is holding its place.

    The holder is part of the answer rather than a detail of it. The only
    reason any of this may be deleted is that something else, filed under the
    serial the hardware states, has already taken the identity it needs. A
    different thing holding it is a different reason, and @rroller's #1146
    review is that nothing may be deleted on a reason nobody was shown.

    ⚠ Recomputed from the registry every time it is asked, and never carried
    in a card's `data`. The #827 merge card deleted 232 entities because it
    acted on a list gathered earlier (see #1114); anything that has changed
    since is simply not in this answer.

    Residue is excluded, because #1145 passes that over rather than refusing,
    so it is not a conflict and must not be offered for deletion here.
    """
    entities = er.async_get(hass)
    devices = dr.async_get(hass)
    entry_rows = er.async_entries_for_config_entry(entities, entry.entry_id)
    entry_devices = dr.async_entries_for_config_entry(devices, entry.entry_id)
    roots = _legacy_roots(entry, entry_rows, entry_devices)
    if not serial or serial in roots:
        return [], []
    replacement = _replacer(roots, serial)

    blocked_rows = []
    for row in entry_rows:
        target = replacement(row.unique_id)
        if target == row.unique_id:
            continue
        held = entities.async_get_entity_id(row.domain, row.platform, target)
        if held is None or held == row.entity_id:
            continue
        holder = entities.async_get(held)
        if holder is None or _is_residue(row, holder):
            continue
        blocked_rows.append((row, holder))

    blocked_devices = []
    for device in entry_devices:
        targets = {
            (domain, replacement(value) if domain == DOMAIN else value)
            for domain, value in device.identifiers
        }
        if targets == device.identifiers:
            continue
        holders = [
            other
            for identifier in targets
            if identifier[0] == DOMAIN
            for other in devices_with_identifier(devices, identifier)
            if other.id != device.id
        ]
        if not holders or all(_is_residue(device, other) for other in holders):
            continue
        blocked_devices.append((device, tuple(sorted(other.id for other in holders))))

    return blocked_rows, blocked_devices


def serial_conflicts(hass: HomeAssistant, entry, serial):
    """The rows and devices that cannot take `serial`, because one is held."""
    blocked_rows, blocked_devices = _conflict_holders(hass, entry, serial)
    return (
        [row for row, _ in blocked_rows],
        [device for device, _ in blocked_devices],
    )


def _removal_plan(hass: HomeAssistant, entry, serial):
    """What would be removed, and every fact the decision rests on.

    The third value is the comparable one. Recomputing the lists across the
    awaited backup is not enough on its own: two reads can return the same
    entity IDs while the identity, the owning entry or the holder behind them
    has changed, and each of those is a reason to stop rather than a detail.
    """
    blocked_rows, blocked_devices = _conflict_holders(hass, entry, serial)
    rows = [row for row, _ in blocked_rows]
    devices = [device for device, _ in blocked_devices]
    signature = (
        tuple(
            sorted(
                (
                    row.entity_id,
                    row.unique_id or "",
                    row.config_entry_id or "",
                    holder.entity_id,
                )
                for row, holder in blocked_rows
            )
        ),
        tuple(
            sorted(
                (device.id, tuple(sorted(device.identifiers)), holders)
                for device, holders in blocked_devices
            )
        ),
        # The bystanders too. Nothing deletes them, but each is moved off a
        # device, which is a thing done to it, and the form lists them. One
        # that arrives while the form is open would otherwise be moved
        # without ever having appeared on it.
        tuple(_collateral(er.async_get(hass), devices, {r.entity_id for r in rows})),
        # And whether this removal would actually finish the job. A config
        # entry filed under the serial can appear while the form is open, or
        # inside the one await there is, and it changes neither list: the
        # same rows would be deleted, for a move that now cannot happen
        # either way (@rroller, #1146). It is part of what was agreed to.
        tuple(serial_adoption_blockers(hass, entry, serial)),
    )
    return rows, devices, signature


def _collateral(entities, blocked_devices, doomed) -> list:
    """Entity IDs that removing those devices would take, uninvited.

    Home Assistant deletes a device's entity rows along with the device.
    @rroller reproduced it on #1146: a legacy device held back by one
    colliding siren row, with a temperature row beside it that could have
    moved, where confirming the siren removed the temperature too.

    Disabled rows included, deliberately. The cascade does not skip them, so
    a count that does would be describing a smaller event than the one about
    to happen, and `async_entries_for_device` leaves them out by default.

    Wider than what the cascade deletes, also deliberately. Home Assistant
    2026.9.3 removes only the rows whose config entry and subentry match the
    device's and detaches the rest itself, so some of these were never in
    danger. Taking them all off is the same end state for those, and the
    safe direction to be wrong in if that rule ever widens. Every one of
    them is listed on the form, which says they are moved and kept.
    """
    return sorted(
        row.entity_id
        for device in blocked_devices
        for row in er.async_entries_for_device(
            entities, device.id, include_disabled_entities=True
        )
        if row.entity_id not in doomed
    )


def serial_conflict_collateral(hass: HomeAssistant, entry, serial) -> list:
    """What the card has to say survives, so the form is the whole story."""
    rows, blocked_devices = serial_conflicts(hass, entry, serial)
    return _collateral(er.async_get(hass), blocked_devices, {r.entity_id for r in rows})


def _adoption_targets(entry, entry_rows, entry_devices, roots, serial):
    """Every row and device that has to move, and what each one becomes.

    One function because the migration and the card have to be asking about
    the same move. They worked it out separately before, and the card's copy
    knew about one of the five conditions the migration refuses on.
    """
    replacement = _replacer(roots, serial)
    rows = [
        (row, replacement(row.unique_id))
        for row in entry_rows
        if replacement(row.unique_id) != row.unique_id
    ]
    stale_devices = [
        (
            device,
            {
                (domain, replacement(value) if domain == DOMAIN else value)
                for domain, value in device.identifiers
            },
        )
        for device in entry_devices
        if any(
            domain == DOMAIN and replacement(value) != value
            for domain, value in device.identifiers
        )
    ]
    return rows, stale_devices, replacement(entry.unique_id)


def _adoption_refusals(
    hass: HomeAssistant,
    entry,
    rows,
    stale_devices,
    new_entry_id,
    ignore_rows=(),
    ignore_devices=(),
):
    """What would move, and EVERY reason the move cannot happen.

    Collected rather than raised, because two callers ask different
    questions of the same five conditions. The migration wants the first
    refusal, in the order it has always printed them, and raises that. The
    card wants to know whether any refusal would still be there after the
    deletion it is about to offer, which is a question about the ones the
    migration never reaches.

    `ignore_rows` and `ignore_devices` are the entity IDs and device IDs a
    removal plan would delete, and each is treated as already gone. That is
    the whole point: a refusal that survives its own removal plan is one no
    button clears, and offering the button anyway is @rroller's #1146
    finding, in each of the shapes it has now been found in.

    Gone as a thing that has to move, and not also as a thing holding
    somebody else's place, because nothing in a plan is ever holding one.
    A row here is filed under a legacy root and the row blocking it is filed
    under the serial, which is not a root, so the blocker is never in the
    plan; devices are the same, and carry one device per identifier besides.
    Both guards for that were written and both were unreachable, which two
    weakenings that reddened nothing is how it came out.

    Order is load-bearing. The migration raises `refusals[0]`, so the
    sequence here is the sequence of messages every existing report maps to.
    """
    entities = er.async_get(hass)
    devices = dr.async_get(hass)
    ignore_rows = set(ignore_rows)
    ignore_devices = set(ignore_devices)
    refusals = []

    # Every refusal below names BOTH sides. @wiiNinja on #1143 upgraded a
    # recorder, got one entity ID and no way to tell what was in the way,
    # and the entry then retried for ever. One side of a collision is not
    # something anybody can act on.
    #
    # They name both sides and stop there. Which duplicate should win is a
    # policy question with real blast radius, and a wrong remedy printed
    # with this much confidence is worse than none: an entity rename in
    # Home Assistant moves the entity ID, not the unique ID the collision
    # is on, so it would not clear anything.
    #
    # None of these messages may carry a unique ID. The identity being
    # moved away from is an md5 of address, RTSP port, username and
    # password, this text is shown on the UI and gets screenshotted into
    # issues, and that would put the password up for an offline attack.
    moving = []
    retained_rows = []
    targets = {}
    for row, target in rows:
        if row.entity_id in ignore_rows:
            continue
        current = entities.async_get(row.entity_id)
        if current is None or current.unique_id != row.unique_id:
            raise ValueError("Serial migration registry changed; retry setup")
        key = (row.domain, row.platform, target)
        claimed = targets.get(key)
        if claimed is not None:
            refusals.append(
                "Serial migration entity conflict: %s and %s would both become "
                "the same entity." % (claimed, row.entity_id)
            )
            continue
        held = entities.async_get_entity_id(*key)
        if held is not None and held != row.entity_id:
            holder = entities.async_get(held)
            # Disabled, and the identity it wants already taken by an
            # enabled one. Nothing is behind it, so it is left alone
            # rather than refused (#1143). Judged on what the registry
            # says NOW, which is why it is judged here.
            if holder is not None and _is_residue(current, holder):
                retained_rows.append(current)
                continue
            refusals.append(
                "Serial migration entity conflict: %s cannot take the identity "
                "it needs because %s already holds it." % (row.entity_id, held)
            )
            continue
        targets[key] = row.entity_id
        moving.append((row, target))

    moving_devices = []
    retained_devices = []
    device_targets = {}
    for device, identifiers in stale_devices:
        if device.id in ignore_devices:
            continue
        current = devices.async_get(device.id)
        if current is None or current.identifiers != device.identifiers:
            raise ValueError("Serial migration registry changed; retry setup")
        holders = [
            other
            for identifier in identifiers
            if identifier[0] == DOMAIN
            for other in devices_with_identifier(devices, identifier)
            if other.id != device.id
        ]
        if holders and all(_is_residue(current, other) for other in holders):
            retained_devices.append(current)
            continue
        refused = False
        for identifier in identifiers:
            if identifier[0] != DOMAIN:
                continue
            claimed = device_targets.get(identifier)
            if claimed is not None:
                refusals.append(
                    "Serial migration device conflict: devices %s and %s would "
                    "both become the same device." % (claimed, _named(device))
                )
                refused = True
                break
            other = next(
                (
                    held
                    for held in devices_with_identifier(devices, identifier)
                    if held.id != device.id
                ),
                None,
            )
            if other is not None:
                refusals.append(
                    "Serial migration device conflict: device %s cannot take the "
                    "identity it needs because device %s already holds it."
                    % (_named(device), _named(other))
                )
                refused = True
                break
            device_targets[identifier] = _named(device)
        if not refused:
            moving_devices.append((device, identifiers))

    if new_entry_id != entry.unique_id:
        other = next(
            (
                candidate
                for candidate in hass.config_entries.async_entries(DOMAIN)
                if candidate.entry_id != entry.entry_id
                and candidate.unique_id == new_entry_id
            ),
            None,
        )
        if other is not None:
            refusals.append(
                "Serial migration config entry conflict: this device cannot take "
                "the identity it reports because the entry %r already holds it."
                % (other.title,)
            )
    return moving, moving_devices, retained_rows, retained_devices, refusals


def serial_adoption_blockers(
    hass: HomeAssistant, entry, serial, after_removing=True
) -> list:
    """Every refusal that would STILL be there once the card has deleted.

    With `after_removing=False`, every refusal there is instead, with
    nothing subtracted, the first of which is what the migration raises.
    The card needs that one to say what stopped the move, and asking here
    rather than keeping a copy is the point: one set of conditions, one
    answer, however it is being asked.

    Something removable being in the way is not the same as removing it
    helping. Three of the five conditions `async_adopt_the_real_serial`
    refuses on are reached only after its row and device loops, so a row or
    device collision is raised first and hides them. The card then offers a
    destructive button, the button works, and the entry comes back refusing
    on a condition no deletion it performs touches.

    So the question is asked properly: replay the whole move with the
    removal plan subtracted, and report what is left. Empty means the button
    really does finish the job.
    """
    entities = er.async_get(hass)
    devices = dr.async_get(hass)
    entry_rows = er.async_entries_for_config_entry(entities, entry.entry_id)
    entry_devices = dr.async_entries_for_config_entry(devices, entry.entry_id)
    roots = _legacy_roots(entry, entry_rows, entry_devices)
    if not serial or serial in roots:
        return []
    rows, stale_devices, new_entry_id = _adoption_targets(
        entry, entry_rows, entry_devices, roots, serial
    )
    doomed_rows, doomed_devices = (
        serial_conflicts(hass, entry, serial) if after_removing else ([], [])
    )
    return _adoption_refusals(
        hass,
        entry,
        rows,
        stale_devices,
        new_entry_id,
        ignore_rows={row.entity_id for row in doomed_rows},
        ignore_devices={device.id for device in doomed_devices},
    )[4]


def serial_conflict_device_labels(hass: HomeAssistant, entry, serial) -> list:
    """Each device that would be removed, beside whatever holds its place.

    The confirm form listed blocked entity ROWS and nothing else, so a
    collision whose blocker is a device asked to remove a count of one and
    printed "(none, only devices)" where the subject should have been. A
    confirmation that does not name what it deletes is the #827 defect this
    card exists to avoid, and @pathia's #1143 report is exactly that shape:
    two device names, no entity row in the message at all.

    Both sides, because the holder is the whole reason any of this may go,
    and a name their owner recognises is what tells a dead leftover from a
    camera still on a wall. Names only, through `_named`: an identifier here
    is the md5 of address, RTSP port, username and password, and this form
    gets screenshotted into issues.

    Recomputed, never carried. Same rule as `_conflict_holders` itself.
    """
    devices = dr.async_get(hass)
    _, blocked_devices = _conflict_holders(hass, entry, serial)
    labels = []
    for device, holder_ids in blocked_devices:
        held = [devices.async_get(holder_id) for holder_id in holder_ids]
        labels.append((_named(device), [_named(h) for h in held if h is not None]))
    return labels


async def async_clear_serial_conflicts(
    hass: HomeAssistant, entry, serial, expected=None
) -> int:
    """Remove the rows that cannot move, so the serial can be adopted.

    The device's own serial wins. Everything removed here is a row filed
    under a generated identity whose place has already been taken by one
    filed under the serial the hardware reports, so the survivor is the one
    the device agrees with.

    A backup of all three registries is taken first, and a failure to take
    one stops the whole thing: this deletes entities somebody may be using,
    and the #827 merge card is why that is not negotiable.

    `expected` is the plan a form put in front of somebody, from
    `_removal_plan`. Pressing the button agrees to that plan and no other.
    The form can sit open for minutes, which is a far wider window than the
    backup, and a row that starts colliding while it is open would otherwise
    be deleted having never been on screen.
    """
    rows, blocked_devices, planned = _removal_plan(hass, entry, serial)
    if not rows and not blocked_devices:
        return 0
    if expected is not None and planned != expected:
        raise SerialConflictPlanChanged(
            "What the form showed is no longer what would happen, so nothing "
            "was removed. Press the button again to see the current list."
        )
    if await _async_backup_once(hass) is None:
        raise ValueError("Could not back up all three registries before removing rows")

    # ⚠ Nothing below this line may await, and the plan below is the one that
    # is acted on. Writing the backup yields, and @rroller reproduced both
    # ends of what that lets through on #1146: remove the holder during the
    # backup, or give the blocked row an identity that no longer collides,
    # and the earlier answer still had it down for deletion. Refusing rather
    # than deleting the smaller set, because the count, the list on the form
    # and the log line all have to describe the same event.
    rows, blocked_devices, current = _removal_plan(hass, entry, serial)
    if current != planned:
        raise SerialConflictPlanChanged(
            "The registry changed while it was being copied, so nothing was "
            "removed. Press the button again to see the current list."
        )

    entities = er.async_get(hass)
    devices = dr.async_get(hass)
    doomed = {row.entity_id for row in rows}
    kept = _collateral(entities, blocked_devices, doomed)
    # Off the device before the device goes, so the cascade cannot reach them.
    # Detached rather than rehomed onto the holder: the entity ID, its history
    # and everything pointing at it survive, and the reload this repair
    # performs re-registers each one with its device, which puts it back.
    # Choosing a new home for them would be guessing, and a device can carry
    # rows this entry does not provide.
    for entity_id in kept:
        entities.async_update_entity(entity_id, device_id=None)
    for row in rows:
        entities.async_remove(row.entity_id)
    for device in blocked_devices:
        devices.async_remove_device(device.id)
    _LOGGER.warning(
        "%s: removed %d registry rows and %d devices that were holding the "
        "reported serial %s out of reach; registries were copied first. %d "
        "entities on those devices were kept and taken off them instead: %s",
        entry.data.get(CONF_ADDRESS),
        len(rows),
        len(blocked_devices),
        serial,
        len(kept),
        ", ".join(kept) or "none",
    )
    return len(rows) + len(blocked_devices)


def _named(device) -> str:
    """Say which device, the way its owner sees it.

    A registry ID is unique and means nothing to anybody reading a setup
    error. The name is what is on the device page. Never the identifier
    itself: that is the credential-derived hash (#1143).
    """
    label = getattr(device, "name_by_user", None) or getattr(device, "name", None)
    return "%s (%s)" % (label, device.id) if label else str(device.id)


async def async_adopt_the_real_serial(hass: HomeAssistant, entry, coordinators) -> int:
    """Adopt a reported serial while preserving entity IDs and device metadata.

    Recognise legacy hashes in this entry's registries even if credentials
    have since changed. Check every target for collisions before changing any
    registry, copy all three persisted registries, then check again after the
    backup yields. Conflicts and backup failures propagate so setup cannot
    create duplicate entities under a partially migrated identity.

    Entity unique IDs move before device identifiers and the config entry ID;
    entity IDs and device IDs remain intact. Return the entity count.
    """
    old = None
    for coordinator in coordinators:
        client = getattr(coordinator, "client", None)
        candidate = getattr(client, "credential_hash", None)
        if candidate is None:
            continue
        old = candidate()
        source = coordinator
        break
    if not old:
        return 0

    entities = er.async_get(hass)
    devices = dr.async_get(hass)
    entry_rows = er.async_entries_for_config_entry(entities, entry.entry_id)
    entry_devices = dr.async_entries_for_config_entry(devices, entry.entry_id)
    # The stored identity may predate a password change. The registries, not
    # today's credentials, are authoritative about which legacy IDs need moving.
    roots = _legacy_roots(entry, entry_rows, entry_devices, extra={old})

    def is_legacy(value):
        return any(
            value == root or (value or "").startswith(root + "_") for root in roots
        )

    if not (
        is_legacy(entry.unique_id)
        or any(is_legacy(row.unique_id) for row in entry_rows)
        or any(
            domain == DOMAIN and is_legacy(value)
            for device in entry_devices
            for domain, value in device.identifiers
        )
    ):
        return 0
    serial = source.get_device_serial_number()
    if not serial or serial in roots:
        return 0

    rows, stale_devices, new_entry_id = _adoption_targets(
        entry, entry_rows, entry_devices, roots, serial
    )
    if not rows and not stale_devices and new_entry_id == entry.unique_id:
        return 0

    def preflight():
        """Check every target, and return what is actually going to move.

        Returns the plan rather than filtering it beforehand, and that
        is the whole point of returning anything. @rroller found on #1145
        that deciding what to pass over BEFORE the awaited backup means
        nothing rechecks it afterwards: enable the dead device during the
        backup, or disable the live one, and the pass-over is no longer
        justified while the migration goes ahead on the earlier answer. The
        second call is the one whose plan is used, and no await separates it
        from the registry writes.

        The conditions themselves live in `_adoption_refusals`, which the
        repair card replays with its own removals subtracted. They were two
        copies of the same rules until @rroller's fourth #1146 review, and
        the card's copy knew about one of the five.
        """
        *plan, refusals = _adoption_refusals(
            hass, entry, rows, stale_devices, new_entry_id
        )
        if refusals:
            # The first one, in the order they have always been printed, so
            # an existing report still maps to the message it quotes.
            raise SerialMigrationConflict(refusals[0])
        return tuple(plan)

    try:
        preflight()
    except SerialMigrationConflict as conflict:
        # The card needs to know which serial is being blocked; it recomputes
        # everything else for itself when the button is pressed.
        conflict.serial = serial
        raise

    backup = await _async_backup_once(hass)
    if backup is None:
        raise ValueError(
            "Could not back up all three registries before serial migration"
        )
    # Backup yields to other entry setups. Recheck every target afterwards,
    # and act on THIS answer rather than the one above; no await separates it
    # from the registry mutations below.
    try:
        moving, moving_devices, retained_rows, retained_devices = preflight()
    except SerialMigrationConflict as conflict:
        conflict.serial = serial
        raise
    if retained_rows or retained_devices:
        # Said plainly, because it is not what the tidy-up card covers.
        # `leftover_rows` skips every row with `disabled_by` set and never
        # looks at the device registry at all, so nothing offers to clear
        # these (@rroller on #1145). They are retained as they are.
        _LOGGER.warning(
            "%s: %d registry rows and %d devices stay on their old identity. "
            "Each one is disabled and the identity it would take is already "
            "held by an enabled one, so there is nothing behind it to move. "
            "Nothing removes them: they are kept exactly as they are",
            entry.data.get(CONF_ADDRESS),
            len(retained_rows),
            len(retained_devices),
        )
    _LOGGER.warning(
        "%s reports serial %s. Moving %d entities and %d devices; persisted "
        "registry files were copied to %s first",
        entry.data.get(CONF_ADDRESS),
        serial,
        len(moving),
        len(moving_devices),
        backup,
    )
    for row, target in moving:
        entities.async_update_entity(row.entity_id, new_unique_id=target)
    for device, identifiers in moving_devices:
        devices.async_update_device(device.id, new_identifiers=identifiers)
    if new_entry_id != entry.unique_id:
        hass.config_entries.async_update_entry(entry, unique_id=new_entry_id)
    return len(moving)
