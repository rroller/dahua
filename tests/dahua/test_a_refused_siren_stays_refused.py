"""A siren the device refused stays refused across a restart (#942).

The AD410 answers `CoaxialControlIO.control` with "Method not found!". Since
#945 the switch goes unavailable after that first refusal, but the memory was
in-process only, so every restart offered the switch again for one more
refused press. Now the refusal is stored per entry with the firmware that gave
it, restored before the platforms are added, and deleted on unload, so a
reload still asks again and a firmware change costs one refusal, as before.
"""

import asyncio
import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store

import custom_components.dahua as integration
from custom_components.dahua import refusal_store, refusals
from custom_components.dahua.rpc2 import Rpc2MethodRefused
from custom_components.dahua.switch import DahuaSirenBinarySwitch

ENTRY_ID = "entry-942"
FIRMWARE = "1.000.0000000.7.R,build:2021-02-20"


@pytest.fixture(autouse=True)
def _clean_refusals():
    refusals.forget()
    refusal_store._LOCKS.clear()
    yield
    refusals.forget()
    refusal_store._LOCKS.clear()


def _coordinator(channel=0, firmware=FIRMWARE):
    return SimpleNamespace(
        get_channel=lambda: channel,
        get_channel_number=lambda: channel + 1,
        get_address=lambda: "10.0.0.7",
        get_firmware_version=lambda: firmware,
        get_device_name=lambda: "Front door",
        uses_recorder_deterrence=lambda: False,
        uses_rpc2_deterrence=lambda *a, **k: True,
        config_entry=SimpleNamespace(entry_id=ENTRY_ID),
        async_refresh=AsyncMock(),
        last_update_success=True,
        client=SimpleNamespace(
            async_set_coaxial_control_state_rpc2=AsyncMock(
                side_effect=Rpc2MethodRefused(
                    "Method not found!",
                    code=268894210,
                    method="CoaxialControlIO.control",
                )
            )
        ),
    )


def _switch(hass, coordinator):
    entity = object.__new__(DahuaSirenBinarySwitch)
    entity._coordinator = coordinator
    entity.coordinator = coordinator
    entity.hass = hass
    return entity


def _restart():
    """What a restart loses: the in-process memory, nothing on disk."""
    refusals.forget()


async def test_a_refusal_survives_a_restart(hass, hass_storage):
    coordinator = _coordinator()
    with pytest.raises(HomeAssistantError):
        await _switch(hass, coordinator).async_turn_on()
    assert refusals.is_refused(coordinator, refusals.SIREN)

    _restart()
    assert not refusals.is_refused(coordinator, refusals.SIREN)
    assert _switch(hass, coordinator).available is True

    await refusal_store.async_restore(hass, ENTRY_ID, [coordinator])

    assert refusals.is_refused(coordinator, refusals.SIREN)
    assert _switch(hass, coordinator).available is False


async def test_a_firmware_change_asks_again(hass, hass_storage):
    coordinator = _coordinator()
    with pytest.raises(HomeAssistantError):
        await _switch(hass, coordinator).async_turn_on()
    _restart()

    upgraded = _coordinator(firmware="1.000.0000001.0.R,build:2026-01-01")
    await refusal_store.async_restore(hass, ENTRY_ID, [upgraded])

    assert not refusals.is_refused(upgraded, refusals.SIREN)


async def test_unload_forgets_the_stored_copy(hass, hass_storage):
    """A reload must ask again, as the warning promises."""
    coordinator = _coordinator()
    with pytest.raises(HomeAssistantError):
        await _switch(hass, coordinator).async_turn_on()

    await refusal_store.async_remove(hass, ENTRY_ID)
    _restart()
    await refusal_store.async_restore(hass, ENTRY_ID, [coordinator])

    assert not refusals.is_refused(coordinator, refusals.SIREN)


async def test_two_channels_refused_at_once_both_stay(hass, hass_storage, monkeypatch):
    """Each save reads the file and writes it back. The load is made to yield,
    so both channels read before either writes, which is the race the lock is
    there for: without it the second write drops the first channel."""
    load = Store.async_load

    async def _yielding_load(self):
        data = await load(self)
        await asyncio.sleep(0)
        return data

    monkeypatch.setattr(Store, "async_load", _yielding_load)
    first, second = _coordinator(channel=0), _coordinator(channel=1)
    await asyncio.gather(
        refusal_store.async_save(hass, ENTRY_ID, first, refusals.SIREN),
        refusal_store.async_save(hass, ENTRY_ID, second, refusals.SIREN),
    )
    _restart()

    await refusal_store.async_restore(hass, ENTRY_ID, [first, second])

    assert refusals.is_refused(first, refusals.SIREN)
    assert refusals.is_refused(second, refusals.SIREN)


async def test_infrared_is_not_kept(hass, hass_storage):
    """An infrared refusal picks a fallback table; keeping it would change
    which table a channel starts on."""
    coordinator = _coordinator()
    await refusal_store.async_save(hass, ENTRY_ID, coordinator, refusals.INFRARED_V1)
    _restart()

    await refusal_store.async_restore(hass, ENTRY_ID, [coordinator])

    assert not refusals.is_refused(coordinator, refusals.INFRARED_V1)


async def test_a_timeout_is_not_kept(hass, hass_storage):
    """Not an outright refusal, so neither remembered nor stored."""
    coordinator = _coordinator()
    coordinator.client.async_set_coaxial_control_state_rpc2.side_effect = (
        asyncio.TimeoutError()
    )
    with pytest.raises(HomeAssistantError):
        await _switch(hass, coordinator).async_turn_on()
    _restart()

    await refusal_store.async_restore(hass, ENTRY_ID, [coordinator])

    assert not refusals.is_refused(coordinator, refusals.SIREN)


def _calls_in_order(function, first, second):
    source = inspect.getsource(function)
    assert first in source and second in source, (first, second)
    return source.index(first) < source.index(second)


def test_setup_restores_before_the_switch_is_added():
    """`available` reads the refusal as the entity is added, so a restore after
    the forward would start the switch available and then flip it."""
    assert _calls_in_order(
        integration.async_setup_entry,
        "await restore_refusals(",
        "async_forward_entry_setups(",
    )


def test_unload_and_removal_delete_the_store():
    for hook in (integration.async_unload_entry, integration.async_remove_entry):
        assert "await remove_stored_refusals(hass, entry.entry_id)" in (
            inspect.getsource(hook)
        ), hook.__name__
