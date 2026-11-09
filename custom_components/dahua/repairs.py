"""Repair flows for Dahua."""

import asyncio
import logging

import voluptuous as vol
from homeassistant.components.repairs import ConfirmRepairFlow, RepairsFlow
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir

from .const import CONF_PORT, CONF_USE_HTTPS, DOMAIN

_LOGGER: logging.Logger = logging.getLogger(__package__)

# An NVR has one config entry per channel, and updating an entry triggers a
# reload through the existing update listener. Reloading eleven at once is the
# same simultaneous burst that can wedge a Dahua web server, so they are done
# one at a time. The per-host request limiter is the hard bound; this is cheap
# insurance on top of it.
RELOAD_STAGGER_SECONDS = 0.5


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict | None
) -> RepairsFlow:
    """Return the flow that fixes this issue."""
    if issue_id.startswith("http_dead_https_available_"):
        return SwitchToHttpsRepairFlow(data or {})
    if issue_id.startswith("siblings_remain_"):
        return RemoveSiblingsRepairFlow(data or {})
    if issue_id.startswith("leftover_rows_"):
        return RemoveLeftoverRowsRepairFlow(data or {})
    if issue_id.startswith("serial_conflict_"):
        return ClearSerialConflictRepairFlow(data or {})
    if issue_id.startswith("webrtc_cannot_play_"):
        return SwitchStreamToH264RepairFlow(data or {})
    return ConfirmRepairFlow()


class SwitchToHttpsRepairFlow(RepairsFlow):
    """Move every entry for one host onto HTTPS on port 443."""

    def __init__(self, data: dict) -> None:
        self._address = data.get("address")

    async def async_step_init(self, user_input: dict | None = None):
        return await self.async_step_confirm()

    async def async_step_confirm(self, user_input: dict | None = None):
        # Imported here rather than at module scope to avoid a circular import.
        from . import _entries_for_address

        entries = _entries_for_address(self.hass, self._address)
        if not entries:
            return self.async_abort(reason="not_configured")

        if user_input is not None:
            for entry in entries:
                # async_update_entry already triggers a reload through the
                # update listener registered in async_setup_entry, so calling
                # async_reload here as well would reload every channel twice.
                self.hass.config_entries.async_update_entry(
                    entry,
                    data={**entry.data, CONF_PORT: "443", CONF_USE_HTTPS: True},
                )
                await asyncio.sleep(RELOAD_STAGGER_SECONDS)

            ir.async_delete_issue(
                self.hass, DOMAIN, f"http_dead_https_available_{self._address}"
            )
            return self.async_create_entry(data={})

        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema({}),
            description_placeholders={
                "address": str(self._address),
                "entries": str(len(entries)),
                "port": str(entries[0].data.get(CONF_PORT, "80")),
            },
        )


class RemoveSiblingsRepairFlow(RepairsFlow):
    """Remove the other entries for one recorder, having removed one of them.

    An NVR is one config entry per channel, so removing "the recorder" is as
    many deletions as it has channels and nobody realises until they are
    part-way through. This finishes it in one step.

    It removes rather than reconfigures, which is the one irreversible thing in
    this file -- so the form lists exactly what will go, by name, and the user
    presses the button. Nothing happens on merely opening the card.
    """

    def __init__(self, data: dict) -> None:
        self._address = data.get("address")
        self._removed = data.get("removed") or "the entry"
        self._dependents_note = data.get("dependents_note") or ""

    async def async_step_init(self, user_input: dict | None = None):
        return await self.async_step_confirm()

    async def async_step_confirm(self, user_input: dict | None = None):
        # Imported here rather than at module scope to avoid a circular import.
        from . import ISSUE_SIBLINGS_REMAIN, _entries_for_address, channel_configs

        entries = _entries_for_address(self.hass, self._address)
        if not entries:
            # Already dealt with by hand, or the last one went while the card
            # was open. Nothing to do and nothing to apologise for.
            ir.async_delete_issue(
                self.hass, DOMAIN, ISSUE_SIBLINGS_REMAIN.format(self._address)
            )
            return self.async_abort(reason="not_configured")

        # Split once, and used by both the form and the removal below, because
        # they disagreed: the loop skipped a merged entry while the form counted
        # and named it. So the card said "This removes the remaining 3 for you"
        # and listed an entry it would not touch -- and where every entry was
        # protected it removed nothing, said it had succeeded, and left the only
        # explanation in the log. For the one irreversible action in this file,
        # the form has to describe what the button does.
        removable = [entry for entry in entries if len(channel_configs(entry)) == 1]
        protected = [entry for entry in entries if len(channel_configs(entry)) > 1]

        if not removable:
            # Nothing this card can do. Saying so is the point: reporting
            # success for having removed nothing is worse than the card not
            # existing, because the user believes the recorder is gone.
            _LOGGER.warning(
                "Not removing anything for %s: the %d entries left each hold "
                "every channel of the recorder, so none of them is a leftover. "
                "Remove it from the integrations page if that is really what "
                "you want",
                self._address,
                len(protected),
            )
            ir.async_delete_issue(
                self.hass, DOMAIN, ISSUE_SIBLINGS_REMAIN.format(self._address)
            )
            return self.async_abort(reason="nothing_to_remove")

        if user_input is not None:
            # A merged recorder is never a leftover. This card offers to finish a
            # deletion the user started, and the entries it is for are the other
            # channels of a recorder that was never merged -- each owning its own
            # channel's entities, which is exactly what the user is asking to be
            # rid of. An entry holding *every* channel of the host is the
            # opposite: removing it deletes everything the recorder has.
            #
            # That happened. The #827 migration's own removals raised this card,
            # and it is persistent, so it outlived the merge and then described the
            # merged entry as a sibling to clean up. Confirming it deleted 232
            # entities. The migration refuses to remove an entry that still owns
            # what it is about to lose; this, the only irreversible action in the
            # file, did not.
            #
            # Counted rather than inferred from `entry.subentries`. That was a
            # proxy for "holds many channels", and it stopped being one when a
            # single camera stopped getting a subentry of its own: a camera added
            # after that change would have read as a merged recorder and been
            # protected from a card the user had asked for. Counting says what the
            # guard has always meant, and is right under either shape.
            if protected:
                _LOGGER.error(
                    "Not removing %s for %s: it holds every channel of the "
                    "recorder on one entry, so it is not a leftover and removing "
                    "it would delete every entity the recorder has. Remove it from "
                    "the integrations page if that is really what you want",
                    ", ".join(sorted(e.title or "untitled" for e in protected)),
                    self._address,
                )

            for entry in removable:
                await self.hass.config_entries.async_remove(entry.entry_id)
                # Same reason the HTTPS flow staggers its reloads: a Dahua web
                # server does not enjoy eleven simultaneous teardowns.
                await asyncio.sleep(RELOAD_STAGGER_SECONDS)

            ir.async_delete_issue(
                self.hass, DOMAIN, ISSUE_SIBLINGS_REMAIN.format(self._address)
            )
            return self.async_create_entry(data={})

        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema({}),
            description_placeholders={
                "address": str(self._address),
                # What will actually go, not what is at this address. A
                # protected entry is named in its own sentence below instead.
                "count": str(len(removable)),
                "titles": ", ".join(sorted(e.title or "untitled" for e in removable)),
                "removed": str(self._removed),
                "dependents_note": self._dependents_note,
                # Built here as prose rather than as a translated string, the
                # same way dependents_note is: a whole sentence or nothing, so
                # the screen reads correctly either way.
                "protected_note": (
                    (
                        "%s holds every channel of the recorder on one entry, so "
                        "it is not a leftover and is left alone. Remove it from "
                        "the integrations page if that is really what you want."
                        % ", ".join(sorted(e.title or "untitled" for e in protected))
                    )
                    if protected
                    else ""
                ),
            },
        )


class ClearSerialConflictRepairFlow(RepairsFlow):
    """Clear the rows standing in the way of the serial the device reports.

    The device's own serial wins, which is the only defensible answer: every
    row removed here is filed under an identity generated from credentials,
    and its place has already been taken by one filed under the serial the
    hardware states. The survivor is the one the device agrees with.

    **The list is recomputed when the button is pressed**, never read out of
    the card's data, for the reason written across `RemoveLeftoverRowsRepairFlow`
    and the #827 merge card: a card that acts on evidence gathered earlier is
    how 232 entities were deleted.
    """

    def __init__(self, data: dict) -> None:
        self._entry_id = data.get("entry_id")
        self._serial = data.get("serial")
        # The plan the form last put on screen. Pressing the button agrees to
        # that one and no other, and a form can sit open for a long time.
        self._shown = None

    async def async_step_init(self, user_input: dict | None = None):
        return await self.async_step_confirm()

    async def _async_nothing_left(self, entry, issue_id):
        """An empty list is not a migration that can now succeed.

        @rroller on #1146: three of the five things the migration refuses on
        are invisible to `_conflict_holders`, which only finds an identity
        somebody else is holding. On those this flow found nothing, deleted
        its own card and reported that nothing was standing in the way, while
        the same migration still failed and nothing retried it.

        So the migration is asked rather than the list. A reload is what the
        success path does anyway and is the only thing that runs the preflight
        again.

        The card goes down first, and what is read afterwards is whether a
        serial conflict card came back up. Not whether the entry loaded: an
        entry can fail to load for reasons that have nothing to do with a
        serial, a device that went offline being the obvious one, and
        answering "still blocked" to that would be pointing at the wrong
        thing. A collision that is still there re-raises and says so itself.
        """
        from . import ISSUE_SERIAL_CONFLICT, ISSUE_SERIAL_CONFLICT_MANUAL

        # Stale either way: it describes a removal with nothing in it.
        ir.async_delete_issue(self.hass, DOMAIN, issue_id)
        await self.hass.config_entries.async_reload(entry.entry_id)
        registry = ir.async_get(self.hass)
        if any(
            registry.async_get_issue(DOMAIN, template.format(self._entry_id))
            is not None
            for template in (ISSUE_SERIAL_CONFLICT, ISSUE_SERIAL_CONFLICT_MANUAL)
        ):
            return self.async_abort(reason="still_blocked")
        return self.async_abort(reason="nothing_to_remove")

    async def _async_cannot_help(self, entry):
        """Something is removable and removing it would not finish the move.

        The card goes up from what is already known here, rather than from
        a reload. `_async_nothing_left` asks the migration because an empty
        list is not evidence of anything; this is the opposite case, where
        the evidence is in hand, and a reload can fail for reasons that have
        nothing to do with a serial. Answering "nothing is standing in the
        way" because a camera was offline is the 08:50 #1146 finding again
        with a different cause.

        One card builder, so the manual card reads the same however it was
        reached, and the refusal it quotes is the one the migration raises.
        """
        from . import _async_offer_to_clear_the_conflict
        from .migrate import SerialMigrationConflict, serial_adoption_blockers

        _LOGGER.warning(
            "Serial conflict repair for %s removed nothing: the move is "
            "blocked by something this cannot clear",
            entry.title,
        )
        every = serial_adoption_blockers(
            self.hass, entry, self._serial, after_removing=False
        )
        conflict = SerialMigrationConflict(every[0])
        conflict.serial = self._serial
        _async_offer_to_clear_the_conflict(self.hass, entry, conflict)
        return self.async_abort(reason="still_blocked")

    async def async_step_confirm(self, user_input: dict | None = None):
        # Imported here rather than at module scope to avoid a circular import.
        from . import ISSUE_SERIAL_CONFLICT
        from .migrate import (
            SerialConflictPlanChanged,
            _removal_plan,
            async_clear_serial_conflicts,
            serial_adoption_blockers,
            serial_conflict_collateral,
            serial_conflict_device_labels,
        )

        issue_id = ISSUE_SERIAL_CONFLICT.format(self._entry_id)
        entry = self.hass.config_entries.async_get_entry(self._entry_id or "")
        if entry is None:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
            return self.async_abort(reason="not_configured")

        rows, devices, plan = _removal_plan(self.hass, entry, self._serial)
        if not rows and not devices:
            return await self._async_nothing_left(entry, issue_id)
        if serial_adoption_blockers(self.hass, entry, self._serial):
            # There is something to remove and removing it would not help.
            # The card was raised before that was true, or was raised
            # correctly and another config entry has taken the serial since;
            # either way a form offering deletion is now the wrong screen,
            # and nothing is deleted on the way to saying so.
            return await self._async_cannot_help(entry)

        errors = None
        if user_input is not None:
            try:
                await async_clear_serial_conflicts(
                    self.hass, entry, self._serial, expected=self._shown
                )
            except SerialConflictPlanChanged:
                # Nothing was removed. Fall through and show the form again,
                # rebuilt from the registry as it is now, because what is on
                # screen is no longer what would happen (@rroller, #1146).
                _LOGGER.warning(
                    "Serial conflict repair for %s stopped without removing "
                    "anything: the registry changed while it was being copied",
                    entry.title,
                )
                errors = {"base": "plan_changed"}
                rows, devices, plan = _removal_plan(self.hass, entry, self._serial)
                if not rows and not devices:
                    return await self._async_nothing_left(entry, issue_id)
                # What changed during the backup may be the thing that makes
                # the whole removal pointless. "Press the button again" is
                # the wrong answer to that, and it is the same screen again.
                if serial_adoption_blockers(self.hass, entry, self._serial):
                    return await self._async_cannot_help(entry)
            else:
                ir.async_delete_issue(self.hass, DOMAIN, issue_id)
                # The entry failed permanently, so nothing is going to retry it.
                await self.hass.config_entries.async_reload(entry.entry_id)
                return self.async_create_entry(data={})

        # Everything the removal touches, not only what it was aimed at.
        # Removing a device takes its entity rows too, so the ones that are
        # being kept are listed as well: a confirmation that names some of
        # what it affects is the defect this is here to close.
        kept = serial_conflict_collateral(self.hass, entry, self._serial)
        # Both halves of what goes. The count has always included devices, and
        # the list never did: a device-shaped collision read "1" and then
        # "(none, only devices)", which is a button whose subject is not on
        # the screen (@pathia, #1143). Names, never identifiers.
        removals = ["- `%s`" % row.entity_id for row in rows[:40]]
        removals += [
            "- the device **%s**, whose identity is held by %s"
            % (label, " and ".join("**%s**" % held for held in holders) or "something")
            for label, holders in serial_conflict_device_labels(
                self.hass, entry, self._serial
            )[:40]
        ]
        # What is about to be on screen is what the next press agrees to.
        self._shown = plan
        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema({}),
            errors=errors,
            description_placeholders={
                "name": entry.title or "untitled",
                "serial": str(self._serial),
                "count": str(len(rows) + len(devices)),
                "removals": "\n".join(removals) or "- (none)",
                "kept": "\n".join("- `%s`" % entity_id for entity_id in kept[:40])
                or "- (none)",
            },
        )


class RemoveLeftoverRowsRepairFlow(RepairsFlow):
    """Remove the registry rows of one entry that no entity is behind.

    Home Assistant already offers this one entity at a time, on each one's own
    settings page. @kmich hit a monitor's worth at once, left behind when
    switches an earlier release created on a VTH were gated off, and doing
    that by hand twenty times is the thing this saves.

    **Written to prefer doing nothing.** Missing a leftover costs a user one
    entity to delete themselves; offering a live one is a bulk delete of
    entities somebody is using, which is what the #827 merge card did when it
    deleted 232 of them. So the list is never carried in the card's `data`,
    which is how that card outlived its evidence: it is recomputed here, at
    the moment the button is pressed, from the same function that raised the
    card. Anything that has come back since is simply not in it any more.
    """

    def __init__(self, data: dict) -> None:
        self._entry_id = data.get("entry_id")
        self._shown_rows = None

    async def async_step_init(self, user_input: dict | None = None):
        return await self.async_step_confirm()

    async def async_step_confirm(self, user_input: dict | None = None):
        # Imported here rather than at module scope to avoid a circular import.
        from . import ISSUE_LEFTOVER_ROWS, entry_coordinators
        from .migrate import _entry_is_wholly_healthy, leftover_rows

        entry = self.hass.config_entries.async_get_entry(self._entry_id or "")
        if entry is None:
            ir.async_delete_issue(
                self.hass, DOMAIN, ISSUE_LEFTOVER_ROWS.format(self._entry_id)
            )
            return self.async_abort(reason="not_configured")

        # Health is re-checked here, not only where the card was raised.
        #
        # Found by @kmich reviewing this, with a repro. Unloading an entry
        # writes every one of its rows as unavailable with `restored: True`
        # (`Entity.async_remove`, the `write_unavailable_state` branch), which
        # `leftover_rows` cannot tell from a real leftover. So between an
        # unload and the end of the next setup -- a window that is tens of
        # seconds on a slow monitor, and unbounded if that setup raises
        # `ConfigEntryNotReady` -- every live entity of the entry reads as a
        # leftover, and a card raised earlier while everything was healthy was
        # still there to be pressed. It removed the lot.
        #
        # The card being recomputed at the button was not enough on its own:
        # it recomputed from a registry that was lying. This asks the question
        # the detection cannot answer, which is whether the integration is
        # currently the reason those rows have no entity.
        if entry.state is not ConfigEntryState.LOADED or not _entry_is_wholly_healthy(
            self.hass, entry, list(entry_coordinators(entry).values())
        ):
            _LOGGER.debug(
                "Not offering leftover rows for %s: the entry is %s and every row "
                "of an entry that is not fully up reads as a leftover",
                entry.title,
                entry.state,
            )
            ir.async_delete_issue(
                self.hass, DOMAIN, ISSUE_LEFTOVER_ROWS.format(entry.entry_id)
            )
            return self.async_abort(reason="not_loaded")

        rows = leftover_rows(self.hass, entry)
        if not rows:
            # Everything came back, or somebody deleted them by hand. Saying so
            # beats reporting success for having removed nothing, which is how
            # a user ends up believing a tidy-up happened.
            ir.async_delete_issue(
                self.hass, DOMAIN, ISSUE_LEFTOVER_ROWS.format(entry.entry_id)
            )
            return self.async_abort(reason="nothing_to_remove")

        if (
            user_input is not None
            and self._shown_rows is not None
            and set(rows).issubset(self._shown_rows)
        ):
            registry = er.async_get(self.hass)
            for entity_id in rows:
                # Re-read each one. The list was taken moments ago, but this is
                # the irreversible step and a row that has gone in between must
                # not raise out of a repair flow.
                if registry.async_get(entity_id) is None:
                    continue
                registry.async_remove(entity_id)
            _LOGGER.warning(
                "Removed %d Dahua registry entries nothing was behind any more: %s",
                len(rows),
                ", ".join(rows),
            )
            ir.async_delete_issue(
                self.hass, DOMAIN, ISSUE_LEFTOVER_ROWS.format(entry.entry_id)
            )
            return self.async_create_entry(data={})

        # A newly missing row must be shown before any confirmation can act
        # on it. Retain this only within the current flow, never in the issue.
        self._shown_rows = set(rows)
        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema({}),
            description_placeholders={
                "name": entry.title or "untitled",
                "count": str(len(rows)),
                # Named in full. This is the one irreversible action here, and
                # a count alone asks somebody to trust a number.
                "entities": "\n".join("- `%s`" % entity_id for entity_id in rows),
            },
        )


def _channel_label(channel: int, coordinator=None) -> str:
    """How one channel is named to the person deciding about it.

    `Channel 3` is an index, and nobody has an index on their wall. The
    recorder's own name for it is what the user recognises, so the picker says
    `Channel 3 (BACKYARD)` and the same label is used in the text above it, so
    the two lists can be compared without counting.

    Falls back to the bare number for a channel with no name, for a name that
    is only the index spelled out, and for a missing coordinator, which is the
    case when a carded channel belongs to an entry that has since gone.
    """
    plain = "Channel %d" % channel
    try:
        name = (coordinator.get_device_name() or "").strip() if coordinator else ""
    except Exception:  # pylint: disable=broad-except
        # Nothing here is worth failing a repair dialog for.
        name = ""
    if not name or name.casefold() == plain.casefold():
        return plain
    return "%s (%s)" % (plain, name)


class SwitchStreamToH264RepairFlow(RepairsFlow):
    """Move the main stream of the carded channels to H.264.

    **The card this fixes describes a conditional failure, and so does this.**
    An H.265 stream with no audio track breaks only for a viewer whose browser
    cannot decode H.265: Home Assistant's fallback for such a browser asks
    go2rtc for the stream's audio-only view, there is no audio track, and the
    offer dies. A browser that takes H.265 directly never touches that path.
    The integration cannot see which kind of browser is watching, so this is
    offered rather than done, and the card says to look first.

    **The main stream is what the device records.** That is why this writes the
    channels the user ticks instead of every channel in the card.

    Like the leftover-rows flow, the list is recomputed at the button rather
    than carried in the card's `data`: a channel somebody fixed on the device
    in the meantime should simply not be offered.
    """

    def __init__(self, data: dict) -> None:
        self._address = data.get("address")
        self._device = data.get("device")

    async def async_step_init(self, user_input: dict | None = None):
        """Two ways out, because one of them used to be missing (#1138).

        @billjes has five H.265 channels and live view working in Chrome and
        the iOS app, so the right answer for him is to change nothing. There
        was no way to say that: a fixable issue offers its flow instead of an
        Ignore, and submitting the picker with nothing ticked left the card
        exactly where it was.

        Submitting an empty picker is deliberately still not the way to say
        it. Ticking nothing is what an accidental Submit looks like, and
        reading it as "never warn me again" would turn a slip into a silent
        permanent setting. It has to be chosen.
        """
        return self.async_show_menu(
            step_id="init", menu_options=["confirm", "keep_h265"]
        )

    async def async_step_keep_h265(self, user_input: dict | None = None):
        """Record that H.265 plays here, and take the card down.

        Written to the config entry's options rather than to a store, so it
        appears in the entry's own settings and can be turned back on in the
        place somebody would look for it.
        """
        from . import CONF_ACCEPT_H265
        from .coordinator import ISSUE_WEBRTC_CANNOT_PLAY

        coordinators = self._coordinators()
        if not coordinators:
            return self.async_abort(reason="not_configured")

        entries = {}
        for coordinator in coordinators.values():
            entry = coordinator.config_entry
            entries[entry.entry_id] = entry
        for entry in entries.values():
            self.hass.config_entries.async_update_entry(
                entry, options={**entry.options, CONF_ACCEPT_H265: True}
            )

        issue_id = ISSUE_WEBRTC_CANNOT_PLAY.format(self._device or self._address)
        ir.async_delete_issue(self.hass, DOMAIN, issue_id)
        return self.async_create_entry(title="", data={})

    def _coordinators(self) -> dict:
        """Every loaded coordinator on this DEVICE, by channel index.

        ⚠ Filtered by device key, not by address, @rroller on #1127.
        `_entries_for_address` is about the network path, and two Dahua boxes
        can sit behind one IP on different ports. Merging them meant one
        recorder's channel 0 overwrote the other's in this mapping, and he
        reproduced the repair writing H.264 to the box on port 8080, calling
        it a success, and clearing the warning that belonged to the box on
        port 80, which was still sending H.265.

        A card raised before this carries no `device`, so there is nothing to
        match on. One address with one device is the overwhelmingly common
        case and behaves as before; the ambiguous case is refused rather than
        guessed, because guessing is what wrote to the wrong recorder.
        """
        # Imported here rather than at module scope to avoid a circular import.
        from . import _entries_for_address, entry_coordinators

        found: dict = {}
        seen_devices = set()
        for entry in _entries_for_address(self.hass, self._address):
            if entry.state is not ConfigEntryState.LOADED:
                continue
            for channel, coordinator in entry_coordinators(entry).items():
                device = getattr(coordinator.client, "device_key", None)
                seen_devices.add(device)
                if self._device is not None and device != self._device:
                    continue
                found[channel] = coordinator

        if self._device is None and len(seen_devices) > 1:
            _LOGGER.warning(
                "Not acting on the unplayable-stream card for %s: %d devices "
                "share that address and the card does not say which. Reload "
                "the entry to raise a card that does",
                self._address,
                len(seen_devices),
            )
            return {}
        return found

    async def _offerable(self, coordinators: dict) -> dict:
        """The carded channels whose encoder actually offers H.264.

        One `getConfigCaps` read for the whole host, taken from whichever
        channel answers, because the reply covers every channel at once and
        asking per channel would be the same 44 KB over and over.

        A channel the caps answer does not mention is left out rather than
        attempted. Absence is not permission: writing a codec the device never
        offered is how a `setConfig` returns 200 with the body `Error`, or
        stores the value and drives nothing.
        """
        from . import dahua_utils

        carded: set = set()
        for coordinator in coordinators.values():
            carded |= coordinator.channels_that_cannot_be_played()
        if not carded:
            return {}

        caps = {}
        for channel in sorted(carded):
            coordinator = coordinators.get(channel)
            if coordinator is None:
                continue
            caps = await coordinator.client.async_get_encode_caps()
            if caps:
                break

        offerable = {}
        for channel in sorted(carded):
            if channel not in coordinators:
                continue
            options = dahua_utils.parse_stream_compression_options(caps, channel)
            if dahua_utils.stream_can_be_set_to(options, 0, "H.264"):
                offerable[channel] = coordinators[channel]
        return offerable

    async def async_step_confirm(self, user_input: dict | None = None):
        # Imported here rather than at module scope to avoid a circular import.
        from .coordinator import ISSUE_WEBRTC_CANNOT_PLAY

        issue_id = ISSUE_WEBRTC_CANNOT_PLAY.format(self._device or self._address)
        coordinators = self._coordinators()
        if not coordinators:
            return self.async_abort(reason="not_configured")

        offerable = await self._offerable(coordinators)
        if not offerable:
            # Either nothing is carded any more, or no carded channel's encoder
            # will take H.264. Neither is something this flow can act on, and
            # the second is not a fault to leave a card standing for.
            _LOGGER.debug(
                "No channel on %s can be offered H.264; not holding the card open",
                self._address,
            )
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
            return self.async_abort(reason="nothing_to_change")

        if user_input is None:
            # ⚠ A fixable issue has no `description` of its own: Home Assistant's
            # translation schema treats `description` and `fix_flow` as mutually
            # exclusive, and hassfest refuses the manifest if both are present.
            # So everything the card used to say lives in this step, and the
            # placeholders it needs have to be supplied here rather than by the
            # `translation_placeholders` the issue was raised with.
            #
            # `channels` is every carded channel, not the offerable subset: the
            # text explains the condition first and the picker second, and a
            # channel that cannot take H.264 is still one the user should know
            # about.
            carded: set = set()
            for coordinator in coordinators.values():
                carded |= coordinator.channels_that_cannot_be_played()
            return self.async_show_form(
                step_id="confirm",
                data_schema=vol.Schema(
                    {
                        vol.Required("channels", default=[]): cv.multi_select(
                            {
                                str(channel): _channel_label(
                                    channel, offerable.get(channel)
                                )
                                for channel in sorted(offerable)
                            }
                        )
                    }
                ),
                description_placeholders={
                    "address": str(self._address),
                    "channels": ", ".join(
                        _channel_label(channel, coordinators.get(channel))
                        for channel in sorted(carded)
                    ),
                },
            )

        chosen = [
            int(channel)
            for channel in user_input.get("channels") or []
            if str(channel).isdigit() and int(channel) in offerable
        ]
        if not chosen:
            # Submitting with nothing ticked is a decision to change nothing,
            # not an error, and the card stays for next time.
            return self.async_abort(reason="nothing_to_change")

        failed = []
        for channel in chosen:
            coordinator = offerable[channel]
            try:
                await coordinator.client.async_set_stream_compression(
                    channel, 0, "H.264"
                )
            except Exception as error:  # pylint: disable=broad-except
                _LOGGER.warning(
                    "Could not set channel %s on %s to H.264: %s",
                    channel,
                    self._address,
                    error,
                )
                failed.append(channel)

        # Read back before saying anything. A Dahua write answering 200 is not
        # the same as the stream having changed: this device family has stored
        # a value, answered OK and driven nothing (#647). The card is then
        # re-judged from what came back, so a write that did not take leaves it
        # standing rather than quietly clearing it.
        for channel in chosen:
            coordinator = offerable[channel]
            try:
                confirmed = await coordinator.async_recheck_stream_codecs()
            except Exception as error:  # pylint: disable=broad-except
                _LOGGER.debug(
                    "Could not re-read the codecs of channel %s on %s: %s",
                    channel,
                    self._address,
                    error,
                )
                confirmed = False
            # ⚠ Unconfirmed is a failure, not a pass, @rroller on #1127. A
            # read-back that said nothing about this channel proves nothing
            # about the write, and treating "no news" as success is how a
            # refused write would be reported as done.
            if channel in failed:
                continue
            if not confirmed or coordinator.get_stream_codec(0) != "H.264":
                failed.append(channel)

        if failed:
            return self.async_abort(reason="write_refused")
        return self.async_create_entry(title="", data={})
