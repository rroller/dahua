"""Repair flows for Dahua."""

import asyncio
import logging

import voluptuous as vol
from homeassistant.components.repairs import ConfirmRepairFlow, RepairsFlow
from homeassistant.core import HomeAssistant
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
