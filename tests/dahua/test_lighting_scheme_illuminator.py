"""IPC-Color4M-TZ needs two complete tables to control its white light."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.dahua import DahuaDataUpdateCoordinator
from custom_components.dahua.client import (
    DahuaClient,
    lighting_scheme_illuminator_tables,
)
from custom_components.dahua.rpc2 import DahuaRpc2Client


def _tables():
    scheme = [[
        {"LightingMode": "AIMode"},
        {"LightingMode": "AIMode"},
    ]]
    lighting = [[
        [
            {"LightType": "InfraredLight", "Mode": "Auto"},
            {
                "LightType": "WhiteLight",
                "Mode": "ZoomPrio",
                "PercentOfMaxBrightness": 20,
                "NearLight": [{"Light": 50}],
                "MiddleLight": [],
                "FarLight": [{"Light": 50}],
            },
        ],
        [
            {"LightType": "InfraredLight", "Mode": "Auto"},
            {
                "LightType": "WhiteLight",
                "Mode": "ZoomPrio",
                "PercentOfMaxBrightness": 20,
                "NearLight": [{"Light": 50}],
                "MiddleLight": [],
                "FarLight": [{"Light": 50}],
            },
        ],
    ]]
    return scheme, lighting


def test_turn_on_selects_white_mode_and_configures_every_white_emitter():
    original_scheme, original_lighting = _tables()
    scheme, lighting = lighting_scheme_illuminator_tables(
        original_scheme, original_lighting, 0, 1, 1, True, 73)

    assert scheme[0][1]["LightingMode"] == "WhiteMode"
    row = lighting[0][1][1]
    assert row["Mode"] == "Manual"
    assert row["PercentOfMaxBrightness"] == 73
    assert row["NearLight"][0]["Light"] == 73
    assert row["FarLight"][0]["Light"] == 73
    assert original_scheme[0][1]["LightingMode"] == "AIMode"
    assert original_lighting[0][1][1]["Mode"] == "ZoomPrio"


@pytest.mark.parametrize("restore_mode", ["AIMode", "InfraredMode"])
def test_turn_off_restores_previous_scheme_without_overwriting_preferences(restore_mode):
    scheme, lighting = _tables()
    scheme[0][1]["LightingMode"] = "WhiteMode"
    lighting[0][1][1]["Mode"] = "Manual"
    before = _tables()[1]

    updated_scheme, updated_lighting = lighting_scheme_illuminator_tables(
        scheme, lighting, 0, 1, 1, False, 100, restore_mode)

    assert updated_scheme[0][1]["LightingMode"] == restore_mode
    assert updated_lighting == lighting
    assert before[0][1][1]["Mode"] == "ZoomPrio"


def test_turn_off_without_saved_mode_does_not_guess_a_scheme():
    scheme, lighting = _tables()
    scheme[0][1]["LightingMode"] = "WhiteMode"
    lighting[0][1][1]["Mode"] = "Manual"

    updated_scheme, updated_lighting = lighting_scheme_illuminator_tables(
        scheme, lighting, 0, 1, 1, False, 100)

    assert updated_scheme[0][1]["LightingMode"] == "WhiteMode"
    assert updated_lighting[0][1][1]["Mode"] == "Off"


def test_turn_off_does_not_touch_an_inactive_scheme_without_saved_mode():
    scheme, lighting = _tables()
    updated_scheme, updated_lighting = lighting_scheme_illuminator_tables(
        scheme, lighting, 0, 1, 1, False, 100)

    assert updated_scheme == scheme
    assert updated_lighting == lighting


@pytest.mark.parametrize("light_index", [0, 3])
def test_wrong_or_missing_white_light_fails_closed(light_index):
    scheme, lighting = _tables()
    with pytest.raises(ValueError):
        lighting_scheme_illuminator_tables(
            scheme, lighting, 0, 1, light_index, True, 100)


async def test_client_commits_both_complete_tables_in_one_rpc2_call(monkeypatch):
    scheme, lighting = _tables()
    rpc2 = SimpleNamespace(
        get_config=AsyncMock(side_effect=[
            {"table": scheme},
            {"table": lighting},
        ]),
        set_configs=AsyncMock(return_value={"result": True}),
    )
    client = object.__new__(DahuaClient)
    client._address = "192.0.2.1"
    client._host_limit = asyncio.Semaphore(1)
    client._lighting_scheme_restore_modes = {}
    client._shared_rpc2 = AsyncMock(return_value=SimpleNamespace(client=rpc2))
    monkeypatch.setattr("custom_components.dahua.client.clear_host_cache", lambda _: None)

    result = await client.async_set_lighting_scheme_illuminator(
        0, True, 66, 1, 1)

    assert result == {"result": True}
    calls = rpc2.set_configs.await_args.args[0]
    assert [name for name, _table in calls] == ["LightingScheme", "Lighting_V2"]
    assert calls[0][1][0][1]["LightingMode"] == "WhiteMode"
    assert calls[1][1][0][1][1]["PercentOfMaxBrightness"] == 66
    assert client._lighting_scheme_restore_modes == {(0, 1): "AIMode"}


async def test_client_preserves_first_mode_across_brightness_changes(monkeypatch):
    scheme, lighting = _tables()
    scheme[0][1]["LightingMode"] = "InfraredMode"
    rpc2 = SimpleNamespace(
        get_config=AsyncMock(side_effect=[{"table": scheme}, {"table": lighting}]),
        set_configs=AsyncMock(return_value={"result": True}),
    )
    client = object.__new__(DahuaClient)
    client._address = "192.0.2.1"
    client._host_limit = asyncio.Semaphore(1)
    client._lighting_scheme_restore_modes = {}
    client._shared_rpc2 = AsyncMock(return_value=SimpleNamespace(client=rpc2))
    monkeypatch.setattr("custom_components.dahua.client.clear_host_cache", lambda _: None)

    await client.async_set_lighting_scheme_illuminator(0, True, 40, 1, 1)
    assert client._lighting_scheme_restore_modes == {(0, 1): "InfraredMode"}

    scheme[0][1]["LightingMode"] = "WhiteMode"
    rpc2.get_config.side_effect = [{"table": scheme}, {"table": lighting}]
    await client.async_set_lighting_scheme_illuminator(0, True, 70, 1, 1)

    assert client._lighting_scheme_restore_modes == {(0, 1): "InfraredMode"}


async def test_client_restores_saved_mode_on_successful_turn_off(monkeypatch):
    scheme, lighting = _tables()
    scheme[0][1]["LightingMode"] = "WhiteMode"
    lighting[0][1][1]["Mode"] = "Manual"
    rpc2 = SimpleNamespace(
        get_config=AsyncMock(side_effect=[{"table": scheme}, {"table": lighting}]),
        set_configs=AsyncMock(return_value={"result": True}),
    )
    client = object.__new__(DahuaClient)
    client._address = "192.0.2.1"
    client._host_limit = asyncio.Semaphore(1)
    client._lighting_scheme_restore_modes = {(0, 1): "InfraredMode"}
    client._shared_rpc2 = AsyncMock(return_value=SimpleNamespace(client=rpc2))
    monkeypatch.setattr("custom_components.dahua.client.clear_host_cache", lambda _: None)

    await client.async_set_lighting_scheme_illuminator(0, False, 70, 1, 1)

    configs = rpc2.set_configs.await_args.args[0]
    assert configs[0][1][0][1]["LightingMode"] == "InfraredMode"
    assert configs[1][1] == lighting
    assert client._lighting_scheme_restore_modes == {}


async def test_failed_turn_off_keeps_saved_mode(monkeypatch):
    scheme, lighting = _tables()
    scheme[0][1]["LightingMode"] = "WhiteMode"
    rpc2 = SimpleNamespace(
        get_config=AsyncMock(side_effect=[{"table": scheme}, {"table": lighting}]),
        set_configs=AsyncMock(side_effect=ConnectionError("write failed")),
    )
    client = object.__new__(DahuaClient)
    client._address = "192.0.2.1"
    client._host_limit = asyncio.Semaphore(1)
    client._lighting_scheme_restore_modes = {(0, 1): "AIMode"}
    client._shared_rpc2 = AsyncMock(return_value=SimpleNamespace(client=rpc2))
    monkeypatch.setattr("custom_components.dahua.client.clear_host_cache", lambda _: None)

    with pytest.raises(ConnectionError):
        await client.async_set_lighting_scheme_illuminator(0, False, 70, 1, 1)

    assert client._lighting_scheme_restore_modes == {(0, 1): "AIMode"}


async def test_failed_turn_on_keeps_mode_for_partial_write_recovery(monkeypatch):
    scheme, lighting = _tables()
    scheme[0][1]["LightingMode"] = "InfraredMode"
    rpc2 = SimpleNamespace(
        get_config=AsyncMock(side_effect=[{"table": scheme}, {"table": lighting}]),
        set_configs=AsyncMock(side_effect=ConnectionError("nested write failed")),
    )
    client = object.__new__(DahuaClient)
    client._address = "192.0.2.1"
    client._host_limit = asyncio.Semaphore(1)
    client._lighting_scheme_restore_modes = {}
    client._shared_rpc2 = AsyncMock(return_value=SimpleNamespace(client=rpc2))
    monkeypatch.setattr("custom_components.dahua.client.clear_host_cache", lambda _: None)

    with pytest.raises(ConnectionError):
        await client.async_set_lighting_scheme_illuminator(0, True, 70, 1, 1)

    assert client._lighting_scheme_restore_modes == {(0, 1): "InfraredMode"}


async def test_lighting_scheme_reads_force_cgi():
    client = object.__new__(DahuaClient)
    client._request = AsyncMock(return_value={
        "table.LightingScheme[0][0].LightingMode": "AIMode"
    })

    result = await client.async_get_lighting_scheme()

    assert result["table.LightingScheme[0][0].LightingMode"] == "AIMode"
    client._request.assert_awaited_once_with(
        "/cgi-bin/configManager.cgi?action=getConfig&name=LightingScheme",
        allow_rpc2=False,
    )


async def test_rpc2_multicall_carries_complete_tables_and_session():
    rpc2 = object.__new__(DahuaRpc2Client)
    rpc2._id = 3
    rpc2._session_id = "session-1"
    rpc2.request = AsyncMock(return_value={
        "result": True,
        "params": [{"result": True}, {"result": True}],
    })
    scheme, lighting = _tables()

    await rpc2.set_configs([
        ("LightingScheme", scheme),
        ("Lighting_V2", lighting),
    ])

    kwargs = rpc2.request.await_args.kwargs
    assert kwargs["method"] == "system.multicall"
    assert [call["id"] for call in kwargs["params"]] == [4, 5]
    assert all(call["session"] == "session-1" for call in kwargs["params"])
    assert [call["params"]["name"] for call in kwargs["params"]] == [
        "LightingScheme",
        "Lighting_V2",
    ]


async def test_rpc2_multicall_rejects_a_nested_failure():
    rpc2 = object.__new__(DahuaRpc2Client)
    rpc2._id = 0
    rpc2._session_id = "session-1"
    rpc2.request = AsyncMock(return_value={
        "result": True,
        "params": [{"result": True}, {"result": False}],
    })
    scheme, lighting = _tables()

    with pytest.raises(ConnectionError):
        await rpc2.set_configs([
            ("LightingScheme", scheme),
            ("Lighting_V2", lighting),
        ])


@pytest.mark.parametrize("params", [
    None,
    [],
    [{"result": True}],
    [{"result": True}, {}],
    [{"result": True}, "OK"],
])
async def test_rpc2_multicall_requires_confirmation_for_every_write(params):
    rpc2 = object.__new__(DahuaRpc2Client)
    rpc2._id = 0
    rpc2._session_id = "session-1"
    rpc2.request = AsyncMock(return_value={"result": True, "params": params})
    scheme, lighting = _tables()

    with pytest.raises(ConnectionError):
        await rpc2.set_configs([
            ("LightingScheme", scheme),
            ("Lighting_V2", lighting),
        ])


def _coordinator(scheme_mode="AIMode", light_mode="ZoomPrio"):
    coordinator = object.__new__(DahuaDataUpdateCoordinator)
    coordinator.model = "IPC-Color4M-TZ"
    coordinator._channel = 0
    coordinator._profile_mode = "1"
    coordinator._supports_lighting_scheme_illuminator = True
    coordinator.data = {
        "table.LightingScheme[0][1].LightingMode": scheme_mode,
        "table.Lighting_V2[0][1][0].LightType": "InfraredLight",
        "table.Lighting_V2[0][1][1].LightType": "WhiteLight",
        "table.Lighting_V2[0][1][1].Mode": light_mode,
        "table.Lighting_V2[0][1][1].PercentOfMaxBrightness": "20",
    }
    return coordinator


def test_coordinator_requires_both_modes_to_report_the_white_light_on():
    assert not _coordinator("AIMode", "Manual").is_illuminator_on()
    assert not _coordinator("WhiteMode", "ZoomPrio").is_illuminator_on()
    assert _coordinator("WhiteMode", "Manual").is_illuminator_on()


def test_coordinator_reads_the_active_profiles_white_brightness():
    assert _coordinator().get_illuminator_brightness() == 51


def test_model_needs_a_successful_scheme_probe_before_exposing_illuminator():
    coordinator = _coordinator()
    assert coordinator.supports_illuminator()
    coordinator._supports_lighting_scheme_illuminator = False
    assert not coordinator.supports_illuminator()
