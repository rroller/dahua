"""Setting an entry up has to undo itself when it fails.

The coordinator takes two things in its constructor -- an aiohttp session, and
a reference on the connection pool shared by every entry for the host -- and
only async_stop gives them back. Nothing reaches async_stop unless the
coordinator makes it into hass.data, and a failed setup never gets that far.

Home Assistant retries a failed setup forever, so anything not given back here
is not leaked once. It is leaked once per retry, for as long as the device is
down, which on an eleven-channel NVR is eleven times over.
"""

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.exceptions import ConfigEntryNotReady
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components import dahua as dahua_module
from custom_components.dahua.const import DOMAIN

ADDRESS = "10.9.9.9"
DATA = {
    "username": "u",
    "password": "p",
    "address": ADDRESS,
    "port": 80,
    "rtsp_port": 554,
    "name": "Cam",
    "channel": 0,
    "events": ["VideoMotion"],
}


@pytest.fixture(autouse=True)
def _clean_connectors():
    dahua_module._HOST_CONNECTORS.clear()
    yield
    dahua_module._HOST_CONNECTORS.clear()


def _refcount(address=ADDRESS):
    holder = dahua_module._HOST_CONNECTORS.get(address)
    return holder[1] if holder else 0


def _entry(hass, address=ADDRESS):
    entry = MockConfigEntry(domain=DOMAIN, data={**DATA, "address": address})
    entry.add_to_hass(hass)
    return entry


def _wedged():
    """A device that answers nothing, which is what makes setup fail."""
    return patch.object(
        dahua_module.DahuaDataUpdateCoordinator,
        "_async_update_data",
        side_effect=Exception("device is not answering"),
    )


def _working():
    return patch.object(
        dahua_module.DahuaDataUpdateCoordinator,
        "_async_update_data",
        return_value={"serialNumber": "S1"},
    )


def _no_platforms(hass):
    return (
        patch.object(dahua_module.DahuaDataUpdateCoordinator, "async_start_event_listener"),
        patch.object(hass.config_entries, "async_forward_entry_setups", AsyncMock()),
        patch.object(
            hass.config_entries, "async_unload_platforms", AsyncMock(return_value=True)
        ),
    )


async def _try_setup(hass, entry):
    try:
        await dahua_module.async_setup_entry(hass, entry)
    except Exception:
        return False
    return True


# --- the failed setup -------------------------------------------------------

async def test_a_failed_setup_gives_the_connector_back(hass):
    entry = _entry(hass)

    with _wedged():
        assert await _try_setup(hass, entry) is False

    assert _refcount() == 0


async def test_retrying_a_wedged_device_does_not_accumulate_references(hass):
    """Home Assistant retries forever. This is the shape of the leak."""
    entry = _entry(hass)

    with _wedged():
        for _ in range(20):
            await _try_setup(hass, entry)

    assert _refcount() == 0, "twenty retries held %d references" % _refcount()


async def test_a_failed_setup_closes_its_session(hass):
    """An unclosed session is not just memory -- aiohttp logs it, loudly."""
    entry = _entry(hass)
    sessions = []
    real_init = dahua_module.DahuaDataUpdateCoordinator.__init__

    def record(self, *args, **kwargs):
        real_init(self, *args, **kwargs)
        sessions.append(self._session)

    with _wedged(), patch.object(
        dahua_module.DahuaDataUpdateCoordinator, "__init__", record
    ):
        await _try_setup(hass, entry)

    assert sessions and all(s.closed for s in sessions)


async def test_a_failed_setup_leaves_no_pool_behind_for_the_host(hass):
    entry = _entry(hass)

    with _wedged():
        await _try_setup(hass, entry)

    assert ADDRESS not in dahua_module._HOST_CONNECTORS


async def test_one_channel_failing_does_not_close_the_pool_for_the_others(hass):
    """Ten channels of an NVR are up and the eleventh is not. The failure must
    give back its own reference and nobody else's."""
    working, failing = _entry(hass), _entry(hass)
    starts, forward, unload = _no_platforms(hass)

    with _working(), starts, forward, unload:
        assert await _try_setup(hass, working) is True
    assert _refcount() == 1

    with _wedged():
        assert await _try_setup(hass, failing) is False

    assert _refcount() == 1
    holder = dahua_module._HOST_CONNECTORS[ADDRESS]
    assert not holder[0].closed, "the surviving channel lost its connection pool"


async def test_the_error_still_reaches_home_assistant(hass):
    """Cleaning up must not swallow the failure.

    Naming the exception matters: asserting merely that something was raised
    is satisfied by any incidental failure further down setup, and would pass
    against a version that cleaned up and then carried on."""
    entry = _entry(hass)
    starts, forward, unload = _no_platforms(hass)

    with _wedged(), starts, forward, unload:
        with pytest.raises(ConfigEntryNotReady):
            await dahua_module.async_setup_entry(hass, entry)


async def test_a_failed_setup_is_not_published_to_hass_data(hass):
    """A coordinator left in hass.data after cleanup is a stopped one that
    every platform would then be handed."""
    entry = _entry(hass)
    starts, forward, unload = _no_platforms(hass)

    with _wedged(), starts, forward, unload:
        await _try_setup(hass, entry)

    assert entry.entry_id not in hass.data.get(DOMAIN, {})


# --- the successful setup, unchanged ---------------------------------------

async def test_a_successful_setup_holds_its_reference(hass):
    entry = _entry(hass)
    starts, forward, unload = _no_platforms(hass)

    with _working(), starts, forward, unload:
        assert await _try_setup(hass, entry) is True

    assert _refcount() == 1


async def test_unloading_gives_it_back(hass):
    entry = _entry(hass)
    starts, forward, unload = _no_platforms(hass)

    with _working(), starts, forward, unload:
        await _try_setup(hass, entry)
        await dahua_module.async_unload_entry(hass, entry)

    assert _refcount() == 0


# --- the update listener ----------------------------------------------------

async def test_reloading_does_not_leave_a_listener_behind(hass, enable_custom_integrations):
    """An entry's update listeners are not cleared on unload, so an unwrapped
    add_update_listener leaves one on every reload -- and then one options
    change fires as many reloads as the entry has ever had.

    Driven through Home Assistant's own setup and unload rather than by calling
    this integration's functions, because it is Home Assistant that runs the
    async_on_unload callbacks and that is the half being tested."""
    entry = _entry(hass)
    starts, forward, unload = _no_platforms(hass)

    with _working(), starts, forward, unload:
        for _ in range(5):
            assert await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()
            assert await hass.config_entries.async_unload(entry.entry_id)
            await hass.async_block_till_done()

    assert len(entry.update_listeners) <= 1, (
        "%d listeners registered: one options change would fire %d reloads"
        % (len(entry.update_listeners), len(entry.update_listeners))
    )


async def test_a_loaded_entry_still_reloads_on_an_options_change(hass):
    """The listener has to survive being wrapped."""
    entry = _entry(hass)
    starts, forward, unload = _no_platforms(hass)

    with _working(), starts, forward, unload:
        await _try_setup(hass, entry)

    assert len(entry.update_listeners) == 1
    assert entry.update_listeners[0] is dahua_module.async_reload_entry


async def test_a_failed_setup_registers_no_listener_at_all(hass):
    entry = _entry(hass)

    with _wedged():
        await _try_setup(hass, entry)

    assert entry.update_listeners == []
