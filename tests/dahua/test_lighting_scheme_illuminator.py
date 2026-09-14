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


def test_turn_off_returns_to_ai_without_overwriting_white_light_preferences():
    scheme, lighting = _tables()
    scheme[0][1]["LightingMode"] = "WhiteMode"
    lighting[0][1][1]["Mode"] = "Manual"
    before = _tables()[1]

    updated_scheme, updated_lighting = lighting_scheme_illuminator_tables(
        scheme, lighting, 0, 1, 1, False, 100)

    assert updated_scheme[0][1]["LightingMode"] == "AIMode"
    assert updated_lighting == lighting
    assert before[0][1][1]["Mode"] == "ZoomPrio"


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
    client._shared_rpc2 = AsyncMock(return_value=SimpleNamespace(client=rpc2))
    monkeypatch.setattr("custom_components.dahua.client.clear_host_cache", lambda _: None)

    result = await client.async_set_lighting_scheme_illuminator(
        0, True, 66, 1, 1)

    assert result == {"result": True}
    calls = rpc2.set_configs.await_args.args[0]
    assert [name for name, _table in calls] == ["LightingScheme", "Lighting_V2"]
    assert calls[0][1][0][1]["LightingMode"] == "WhiteMode"
    assert calls[1][1][0][1][1]["PercentOfMaxBrightness"] == 66


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
