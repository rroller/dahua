"""Does a failed setup give the shared connector back?"""
import pytest
from unittest.mock import patch

from homeassistant.exceptions import ConfigEntryNotReady
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components import dahua as dahua_module
from custom_components.dahua.const import DOMAIN

DATA = {
    "username": "u", "password": "p", "address": "10.9.9.9",
    "port": 80, "rtsp_port": 554, "name": "Cam", "channel": 0,
    "events": ["VideoMotion"],
}


@pytest.fixture(autouse=True)
def _clean():
    dahua_module._HOST_CONNECTORS.clear()
    yield
    dahua_module._HOST_CONNECTORS.clear()


def _refcount(address="10.9.9.9"):
    holder = dahua_module._HOST_CONNECTORS.get(address)
    return holder[1] if holder else 0


async def test_a_failed_setup_gives_the_connector_back(hass):
    entry = MockConfigEntry(domain=DOMAIN, data=DATA)
    entry.add_to_hass(hass)

    with patch.object(
        dahua_module.DahuaDataUpdateCoordinator, "_async_update_data",
        side_effect=Exception("device is wedged"),
    ):
        for attempt in range(5):
            try:
                await dahua_module.async_setup_entry(hass, entry)
            except (ConfigEntryNotReady, Exception):
                pass
            print("after attempt %d: refcount=%d" % (attempt + 1, _refcount()))

    assert _refcount() == 0, "five failed setups leaked %d references" % _refcount()


async def test_the_update_listener_is_removed_on_unload(hass):
    entry = MockConfigEntry(domain=DOMAIN, data=DATA)
    entry.add_to_hass(hass)

    with patch.object(
        dahua_module.DahuaDataUpdateCoordinator, "_async_update_data",
        return_value={"serialNumber": "S1"},
    ), patch.object(dahua_module.DahuaDataUpdateCoordinator, "async_start_event_listener"):
        for cycle in range(4):
            await dahua_module.async_setup_entry(hass, entry)
            print("after setup %d: listeners=%d" % (cycle + 1, len(entry.update_listeners)))
            await dahua_module.async_unload_entry(hass, entry)
            print("after unload %d: listeners=%d" % (cycle + 1, len(entry.update_listeners)))

    assert len(entry.update_listeners) <= 1, (
        "%d listeners are registered; one options change fires that many reloads"
        % len(entry.update_listeners)
    )
