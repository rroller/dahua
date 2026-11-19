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


@pytest.mark.parametrize("answered_during", ["removal", "stop"])
async def test_a_refusal_answered_during_a_reload_is_not_kept(
    hass, hass_storage, monkeypatch, answered_during
):
    """rroller on #1228: a press already waiting on the device when a reload
    starts. Its refusal arrives inside the real unload hook, after the hook has
    forgotten the channel and removed the file, and before it stops the
    coordinator. Neither the coordinator that replaces it nor the next restart
    may see it, or the reload would not ask again.

    Answered as the file is being removed, and answered while the coordinator
    stops."""
    gate = asyncio.Event()

    async def _refuse_when_released(*args, **kwargs):
        await gate.wait()
        raise Rpc2MethodRefused(
            "Method not found!", code=268894210, method="CoaxialControlIO.control"
        )

    old = _coordinator()
    old.client.async_set_coaxial_control_state_rpc2.side_effect = _refuse_when_released
    press = asyncio.ensure_future(_switch(hass, old).async_turn_on())
    await asyncio.sleep(0)
    assert not press.done()

    # The hook imports this when it runs, so patching the module reaches it.
    # Not Store.async_remove: hass_storage mocks that, and a patch over the
    # mock is restored after it, leaking the mock into later tests.
    remove = refusal_store.async_remove

    async def _remove_releasing(*args):
        if answered_during == "removal":
            gate.set()
            for _ in range(5):
                await asyncio.sleep(0)
        await remove(*args)

    monkeypatch.setattr(refusal_store, "async_remove", _remove_releasing)

    async def _stop():
        # Inside the hook, after the forgetting and the removal.
        gate.set()
        with pytest.raises(HomeAssistantError):
            await press

    old.async_stop = _stop
    old.platforms = []
    entry = SimpleNamespace(entry_id=ENTRY_ID, runtime_data={0: old})
    assert await integration.async_unload_entry(hass, entry) is True
    assert press.done()

    replacement = _coordinator()
    assert not refusals.is_refused(replacement, refusals.SIREN)
    _restart()
    await refusal_store.async_restore(hass, ENTRY_ID, [replacement])
    assert not refusals.is_refused(replacement, refusals.SIREN)


def _stored(hass_storage, data):
    key = refusal_store.storage_key(ENTRY_ID)
    hass_storage[key] = {
        "version": refusal_store.STORAGE_VERSION,
        "minor_version": 1,
        "key": key,
        "data": data,
    }


@pytest.mark.parametrize("data", [["siren"], "siren", 7])
async def test_a_stored_file_of_the_wrong_shape_is_ignored(hass, hass_storage, data):
    """Valid JSON, wrong shape. A raise here stops the entry forwarding any
    platform, so it must restore nothing and set up normally."""
    _stored(hass_storage, data)
    coordinator = _coordinator()

    await refusal_store.async_restore(hass, ENTRY_ID, [coordinator])

    assert not refusals.is_refused(coordinator, refusals.SIREN)


@pytest.mark.parametrize("row", [["siren"], "siren", 7])
async def test_a_malformed_channel_does_not_cost_its_neighbour(hass, hass_storage, row):
    """rroller on #1228: a row of ["siren"] beside a valid channel."""
    _stored(hass_storage, {"0": row, "1": {refusals.SIREN: FIRMWARE}})
    broken, valid = _coordinator(channel=0), _coordinator(channel=1)

    await refusal_store.async_restore(hass, ENTRY_ID, [broken, valid])

    assert not refusals.is_refused(broken, refusals.SIREN)
    assert refusals.is_refused(valid, refusals.SIREN)


@pytest.mark.parametrize(
    "data", [["siren"], {"0": ["siren"], "1": {"siren": FIRMWARE}}]
)
async def test_a_save_over_a_malformed_file_still_stores(hass, hass_storage, data):
    """A save replaces what it cannot read rather than failing on it, and keeps
    a valid neighbour where there is one."""
    _stored(hass_storage, data)
    coordinator = _coordinator(channel=0)

    await refusal_store.async_save(hass, ENTRY_ID, coordinator, refusals.SIREN)
    _restart()
    await refusal_store.async_restore(hass, ENTRY_ID, [coordinator])

    assert refusals.is_refused(coordinator, refusals.SIREN)
    if isinstance(data, dict):
        neighbour = _coordinator(channel=1)
        await refusal_store.async_restore(hass, ENTRY_ID, [neighbour])
        assert refusals.is_refused(neighbour, refusals.SIREN)
