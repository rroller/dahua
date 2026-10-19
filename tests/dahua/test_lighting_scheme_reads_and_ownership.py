"""Illumination selects read real state and respect the light's recovery owner."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.dahua.client import DahuaClient
from custom_components.dahua.select import DahuaLightingSchemeSelect
from tests.dahua.test_poll_skips_unused import _coordinator


@pytest.mark.parametrize("light_enabled", [False, True])
async def test_illumination_select_reads_the_channel_even_without_lights(light_enabled):
    coordinator = _coordinator(light=light_enabled, select=True)
    coordinator._lighting_schemes = ["WhiteMode", "AIMode"]
    coordinator.data = {}
    coordinator.client.async_get_channel_lighting_scheme = AsyncMock(
        return_value={"table.LightingScheme[0][0].LightingMode": "AIMode"}
    )
    entity = object.__new__(DahuaLightingSchemeSelect)
    entity._coordinator = coordinator
    coordinator.data = await coordinator._async_update_data()
    assert entity.current_option == "smart_dual"
    coordinator.client.async_get_channel_lighting_scheme.assert_awaited_once_with(0)


async def test_override_ownership_is_checked_after_waiting_for_the_device_lock():
    store = SimpleNamespace(async_get=AsyncMock(return_value=None))
    client = DahuaClient(
        "u", "p", "cam", 80, 554, object(), channel_snapshot_store=store
    )
    rpc = SimpleNamespace(
        get_config=AsyncMock(return_value={"table": [{"LightingMode": "AIMode"}]}),
        set_config=AsyncMock(),
    )

    async def call(operation):
        return await operation(rpc)

    client._rpc2_shared_call = call
    await client._lighting_scheme_lock.acquire()
    task = asyncio.create_task(
        client.async_set_channel_scoped_lighting_scheme(11, "AIMode")
    )
    for _ in range(5):
        await asyncio.sleep(0)
    # A concurrent Illuminator ON captured its recovery record while holding
    # the lock. The select must inspect that record after it acquires the lock.
    store.async_get.return_value = {"original": "InfraredMode"}
    client._lighting_scheme_lock.release()
    with pytest.raises(HomeAssistantError) as raised:
        await task
    assert raised.value.translation_key == "lighting_scheme_held_by_illuminator"
    rpc.set_config.assert_not_awaited()


async def test_channel_read_preserves_the_requested_channel_in_flat_keys():
    client = DahuaClient("u", "p", "cam", 80, 554, object())
    rpc = SimpleNamespace(
        get_config=AsyncMock(return_value={"table": [{"LightingMode": "AIMode"}]})
    )

    async def call(operation):
        return await operation(rpc)

    client._rpc2_shared_call = call
    assert await client.async_get_channel_lighting_scheme(11) == {
        "table.LightingScheme[11][0].LightingMode": "AIMode"
    }
    rpc.get_config.assert_awaited_once_with(
        {"name": "LightingScheme", "onlyLocal": False, "channel": 11}
    )


async def test_partial_readback_does_not_verify_all_scenes():
    client = DahuaClient("u", "p", "cam", 80, 554, object())
    rpc = SimpleNamespace(
        get_config=AsyncMock(
            side_effect=[
                {"table": [{"LightingMode": "WhiteMode"}] * 9},
                {"table": [{"LightingMode": "AIMode"}]},
            ]
        ),
        set_config=AsyncMock(),
    )

    async def call(operation):
        return await operation(rpc)

    client._rpc2_shared_call = call
    with pytest.raises(HomeAssistantError) as raised:
        await client.async_set_channel_scoped_lighting_scheme(11, "AIMode")
    assert raised.value.translation_key == "lighting_scheme_not_accepted"
    rpc.set_config.assert_awaited_once_with(
        "LightingScheme", [{"LightingMode": "AIMode"}] * 9, channel=11
    )


async def test_a_failed_scene_read_carries_only_this_channels_previous_state():
    coordinator = _coordinator(select=True, light=False)
    coordinator.data = {
        "table.LightingScheme[0][0].LightingMode": "AIMode",
        "table.LightingScheme[1][0].LightingMode": "WhiteMode",
        "status.Other": "stale",
    }
    coordinator.client.async_get_channel_lighting_scheme = AsyncMock(
        side_effect=ValueError("unreadable")
    )
    assert await coordinator._async_fetch_channel_lighting_scheme() == {
        "table.LightingScheme[0][0].LightingMode": "AIMode"
    }
