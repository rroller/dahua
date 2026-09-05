"""Repair flows for Dahua."""
import asyncio

import voluptuous as vol
from homeassistant.components.repairs import ConfirmRepairFlow, RepairsFlow
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .const import CONF_PORT, CONF_USE_HTTPS, DOMAIN

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
