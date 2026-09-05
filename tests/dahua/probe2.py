import pytest
from unittest.mock import AsyncMock, patch
from pytest_homeassistant_custom_component.common import MockConfigEntry
from custom_components import dahua as dahua_module
from custom_components.dahua.const import DOMAIN

DATA = {"username": "u", "password": "p", "address": "10.9.9.9", "port": 80,
        "rtsp_port": 554, "name": "Cam", "channel": 0, "events": ["VideoMotion"]}


async def test_listeners_accumulate_across_reloads(hass):
    entry = MockConfigEntry(domain=DOMAIN, data=DATA)
    entry.add_to_hass(hass)

    with patch.object(dahua_module.DahuaDataUpdateCoordinator, "_async_update_data",
                      return_value={"serialNumber": "S1"}), \
         patch.object(dahua_module.DahuaDataUpdateCoordinator, "async_start_event_listener"), \
         patch.object(hass.config_entries, "async_forward_entry_setups", AsyncMock()), \
         patch.object(hass.config_entries, "async_unload_platforms", AsyncMock(return_value=True)):
        for cycle in range(4):
            await dahua_module.async_setup_entry(hass, entry)
            await dahua_module.async_unload_entry(hass, entry)
            print("after reload %d: update_listeners=%d, connector_refcount=%d"
                  % (cycle + 1, len(entry.update_listeners),
                     dahua_module._HOST_CONNECTORS.get("10.9.9.9", [None, 0])[1]))

    assert len(entry.update_listeners) <= 1, (
        "%d listeners registered: one options change now fires %d reloads"
        % (len(entry.update_listeners), len(entry.update_listeners)))
