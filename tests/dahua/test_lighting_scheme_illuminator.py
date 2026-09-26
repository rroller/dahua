"""IPC-Color4M-TZ needs two complete tables to control its white light."""

import asyncio
import functools
import logging
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.util.file import WriteError

from custom_components.dahua import DahuaDataUpdateCoordinator
from custom_components.dahua.client import (
    DahuaClient,
    lighting_scheme_illuminator_tables,
)
from custom_components.dahua.illuminator_restore import IlluminatorRestoreStore
from custom_components.dahua.rpc2 import DahuaRpc2Client


class _RestoreStore:
    def __init__(self, modes=None, save_error=None):
        self.modes = dict(modes or {})
        self.save_error = save_error

    async def async_get(self, channel, profile):
        return self.modes.get((channel, profile))

    async def async_keys(self):
        return list(self.modes)

    async def async_set(self, channel, profile, mode):
        if self.save_error is not None:
            raise self.save_error
        self.modes[(channel, profile)] = mode

    async def async_remove(self, channel, profile):
        self.modes.pop((channel, profile), None)


def _client(rpc2, restore_store=None):
    client = object.__new__(DahuaClient)
    client._address = "192.0.2.1"
    client._device = "192.0.2.1:80"
    client._host_limit = asyncio.Semaphore(1)
    client._lighting_scheme_lock = asyncio.Lock()
    client._illuminator_restore_store = restore_store or _RestoreStore()
    client._shared_rpc2 = AsyncMock(return_value=SimpleNamespace(client=rpc2))
    return client


def _tables():
    scheme = [
        [
            {"LightingMode": "AIMode"},
            {"LightingMode": "AIMode"},
        ]
    ]
    lighting = [
        [
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
        ]
    ]
    return scheme, lighting


def test_turn_on_selects_white_mode_and_configures_every_white_emitter():
    original_scheme, original_lighting = _tables()
    scheme, lighting = lighting_scheme_illuminator_tables(
        original_scheme, original_lighting, 0, 1, 1, True, 73
    )

    assert scheme[0][1]["LightingMode"] == "WhiteMode"
    row = lighting[0][1][1]
    assert row["Mode"] == "Manual"
    assert row["PercentOfMaxBrightness"] == 73
    assert row["NearLight"][0]["Light"] == 73
    assert row["FarLight"][0]["Light"] == 73
    assert original_scheme[0][1]["LightingMode"] == "AIMode"
    assert original_lighting[0][1][1]["Mode"] == "ZoomPrio"


@pytest.mark.parametrize("restore_mode", ["AIMode", "InfraredMode"])
def test_turn_off_restores_previous_scheme_without_overwriting_preferences(
    restore_mode,
):
    scheme, lighting = _tables()
    scheme[0][1]["LightingMode"] = "WhiteMode"
    lighting[0][1][1]["Mode"] = "Manual"
    before = _tables()[1]

    updated_scheme, updated_lighting = lighting_scheme_illuminator_tables(
        scheme, lighting, 0, 1, 1, False, 100, restore_mode
    )

    assert updated_scheme[0][1]["LightingMode"] == restore_mode
    assert updated_lighting == lighting
    assert before[0][1][1]["Mode"] == "ZoomPrio"


def test_turn_off_without_saved_mode_does_not_guess_a_scheme():
    scheme, lighting = _tables()
    scheme[0][1]["LightingMode"] = "WhiteMode"
    lighting[0][1][1]["Mode"] = "Manual"

    updated_scheme, updated_lighting = lighting_scheme_illuminator_tables(
        scheme, lighting, 0, 1, 1, False, 100
    )

    assert updated_scheme[0][1]["LightingMode"] == "WhiteMode"
    assert updated_lighting[0][1][1]["Mode"] == "Off"


def test_turn_off_does_not_touch_an_inactive_scheme_without_saved_mode():
    scheme, lighting = _tables()
    updated_scheme, updated_lighting = lighting_scheme_illuminator_tables(
        scheme, lighting, 0, 1, 1, False, 100
    )

    assert updated_scheme == scheme
    assert updated_lighting == lighting


@pytest.mark.parametrize("light_index", [0, 3])
def test_wrong_or_missing_white_light_fails_closed(light_index):
    scheme, lighting = _tables()
    with pytest.raises(ValueError):
        lighting_scheme_illuminator_tables(
            scheme, lighting, 0, 1, light_index, True, 100
        )


async def test_client_commits_both_complete_tables_in_one_rpc2_call(monkeypatch):
    scheme, lighting = _tables()
    rpc2 = SimpleNamespace(
        get_config=AsyncMock(
            side_effect=[
                {"table": scheme},
                {"table": lighting},
            ]
        ),
        set_configs=AsyncMock(return_value={"result": True}),
    )
    restore_store = _RestoreStore()
    client = _client(rpc2, restore_store)
    monkeypatch.setattr(
        "custom_components.dahua.client.clear_host_cache", lambda _: None
    )

    result = await client.async_set_lighting_scheme_illuminator(0, True, 66, 1, 1)

    assert result == {"result": True}
    calls = rpc2.set_configs.await_args.args[0]
    assert [name for name, _table in calls] == ["LightingScheme", "Lighting_V2"]
    assert calls[0][1][0][1]["LightingMode"] == "WhiteMode"
    assert calls[1][1][0][1][1]["PercentOfMaxBrightness"] == 66
    assert restore_store.modes == {(0, 1): "AIMode"}


async def test_client_preserves_first_mode_across_brightness_changes(monkeypatch):
    scheme, lighting = _tables()
    scheme[0][1]["LightingMode"] = "InfraredMode"
    rpc2 = SimpleNamespace(
        get_config=AsyncMock(side_effect=[{"table": scheme}, {"table": lighting}]),
        set_configs=AsyncMock(return_value={"result": True}),
    )
    restore_store = _RestoreStore()
    client = _client(rpc2, restore_store)
    monkeypatch.setattr(
        "custom_components.dahua.client.clear_host_cache", lambda _: None
    )

    await client.async_set_lighting_scheme_illuminator(0, True, 40, 1, 1)
    assert restore_store.modes == {(0, 1): "InfraredMode"}

    scheme[0][1]["LightingMode"] = "WhiteMode"
    rpc2.get_config.side_effect = [{"table": scheme}, {"table": lighting}]
    await client.async_set_lighting_scheme_illuminator(0, True, 70, 1, 1)

    assert restore_store.modes == {(0, 1): "InfraredMode"}


async def test_client_restores_saved_mode_on_successful_turn_off(monkeypatch):
    scheme, lighting = _tables()
    scheme[0][1]["LightingMode"] = "WhiteMode"
    lighting[0][1][1]["Mode"] = "Manual"
    rpc2 = SimpleNamespace(
        get_config=AsyncMock(side_effect=[{"table": scheme}, {"table": lighting}]),
        set_configs=AsyncMock(return_value={"result": True}),
    )
    restore_store = _RestoreStore({(0, 1): "InfraredMode"})
    client = _client(rpc2, restore_store)
    monkeypatch.setattr(
        "custom_components.dahua.client.clear_host_cache", lambda _: None
    )

    await client.async_set_lighting_scheme_illuminator(0, False, 70, 1, 1)

    configs = rpc2.set_configs.await_args.args[0]
    assert configs[0][1][0][1]["LightingMode"] == "InfraredMode"
    assert configs[1][1] == lighting
    assert restore_store.modes == {}


async def test_failed_turn_off_keeps_saved_mode(monkeypatch):
    scheme, lighting = _tables()
    scheme[0][1]["LightingMode"] = "WhiteMode"
    rpc2 = SimpleNamespace(
        get_config=AsyncMock(side_effect=[{"table": scheme}, {"table": lighting}]),
        set_configs=AsyncMock(side_effect=ConnectionError("write failed")),
    )
    restore_store = _RestoreStore({(0, 1): "AIMode"})
    client = _client(rpc2, restore_store)
    monkeypatch.setattr(
        "custom_components.dahua.client.clear_host_cache", lambda _: None
    )

    with pytest.raises(ConnectionError):
        await client.async_set_lighting_scheme_illuminator(0, False, 70, 1, 1)

    assert restore_store.modes == {(0, 1): "AIMode"}


async def test_failed_turn_on_keeps_mode_for_partial_write_recovery(monkeypatch):
    scheme, lighting = _tables()
    scheme[0][1]["LightingMode"] = "InfraredMode"
    rpc2 = SimpleNamespace(
        get_config=AsyncMock(side_effect=[{"table": scheme}, {"table": lighting}]),
        set_configs=AsyncMock(side_effect=ConnectionError("nested write failed")),
    )
    restore_store = _RestoreStore()
    client = _client(rpc2, restore_store)
    monkeypatch.setattr(
        "custom_components.dahua.client.clear_host_cache", lambda _: None
    )

    with pytest.raises(ConnectionError):
        await client.async_set_lighting_scheme_illuminator(0, True, 70, 1, 1)

    assert restore_store.modes == {(0, 1): "InfraredMode"}


async def test_restore_mode_survives_integration_reload(hass):
    first = IlluminatorRestoreStore(hass, "entry-1")
    await first.async_set(0, 1, "InfraredMode")

    reloaded = IlluminatorRestoreStore(hass, "entry-1")
    other_entry = IlluminatorRestoreStore(hass, "entry-2")

    assert await reloaded.async_get(0, 1) == "InfraredMode"
    assert await other_entry.async_get(0, 1) is None


def test_restore_store_uses_atomic_writes(hass):
    restore_store = IlluminatorRestoreStore(hass, "entry-atomic")

    assert restore_store._store._atomic_writes is True


async def test_client_restores_mode_after_integration_reload(hass, monkeypatch):
    before_restart = IlluminatorRestoreStore(hass, "entry-1")
    await before_restart.async_set(0, 1, "InfraredMode")
    after_restart = IlluminatorRestoreStore(hass, "entry-1")
    scheme, lighting = _tables()
    scheme[0][1]["LightingMode"] = "WhiteMode"
    rpc2 = SimpleNamespace(
        get_config=AsyncMock(side_effect=[{"table": scheme}, {"table": lighting}]),
        set_configs=AsyncMock(return_value={"result": True}),
    )
    client = _client(rpc2, after_restart)
    monkeypatch.setattr(
        "custom_components.dahua.client.clear_host_cache", lambda _: None
    )

    await client.async_set_lighting_scheme_illuminator(0, False, 70, 1, 1)

    configs = rpc2.set_configs.await_args.args[0]
    assert configs[0][1][0][1]["LightingMode"] == "InfraredMode"
    assert await IlluminatorRestoreStore(hass, "entry-1").async_get(0, 1) is None


async def test_storage_failure_blocks_persistent_takeover(monkeypatch):
    scheme, lighting = _tables()
    rpc2 = SimpleNamespace(
        get_config=AsyncMock(side_effect=[{"table": scheme}, {"table": lighting}]),
        set_configs=AsyncMock(return_value={"result": True}),
    )
    restore_store = _RestoreStore(save_error=OSError("disk full"))
    client = _client(rpc2, restore_store)
    monkeypatch.setattr(
        "custom_components.dahua.client.clear_host_cache", lambda _: None
    )

    with pytest.raises(OSError, match="disk full"):
        await client.async_set_lighting_scheme_illuminator(0, True, 70, 1, 1)

    rpc2.set_configs.assert_not_awaited()
    assert restore_store.modes == {}


async def test_real_store_write_failure_is_detected(hass):
    restore_store = IlluminatorRestoreStore(hass, "entry-write-failure")
    restore_store._store._async_write_data = AsyncMock(
        side_effect=WriteError("disk full")
    )

    with pytest.raises(RuntimeError, match="verify saved"):
        await restore_store.async_set(0, 1, "InfraredMode")

    assert (
        await IlluminatorRestoreStore(hass, "entry-write-failure").async_get(0, 1)
        is None
    )


async def test_real_store_clear_failure_retains_recovery_mode(hass):
    original = IlluminatorRestoreStore(hass, "entry-clear-failure")
    await original.async_set(0, 1, "InfraredMode")
    restore_store = IlluminatorRestoreStore(hass, "entry-clear-failure")
    restore_store._store._async_write_data = AsyncMock(
        side_effect=WriteError("disk full")
    )

    with pytest.raises(RuntimeError, match="verify cleared"):
        await restore_store.async_remove(0, 1)

    assert (
        await IlluminatorRestoreStore(hass, "entry-clear-failure").async_get(0, 1)
        == "InfraredMode"
    )


async def test_cancelled_store_write_finishes_before_newer_write(hass):
    restore_store = IlluminatorRestoreStore(hass, "entry-cancelled-write")
    await restore_store.async_set(0, 1, "AIMode")

    original_write_data = restore_store._store._write_data
    original_async_write_data = restore_store._store._async_write_data
    delete_started = threading.Event()
    finish_delete = threading.Event()
    block_next_write = True

    def blocking_write_data(*args, **kwargs):
        nonlocal block_next_write
        if block_next_write:
            block_next_write = False
            delete_started.set()
            assert finish_delete.wait(5)
        original_write_data(*args, **kwargs)

    async def executor_write_data(*args, **kwargs):
        if kwargs:
            await hass.async_add_executor_job(
                functools.partial(blocking_write_data, *args, **kwargs)
            )
        else:
            await hass.async_add_executor_job(blocking_write_data, *args)

    restore_store._store._async_write_data = executor_write_data
    remove_task = asyncio.create_task(restore_store.async_remove(0, 1))
    set_task = None
    try:
        async with asyncio.timeout(5):
            while not delete_started.is_set():
                if remove_task.done():
                    remove_task.result()
                await asyncio.sleep(0)

        remove_task.cancel()
        await asyncio.sleep(0)
        set_task = asyncio.create_task(restore_store.async_set(0, 1, "InfraredMode"))
        await asyncio.sleep(0)

        assert restore_store._lock.locked()
        assert not remove_task.done()
        assert not set_task.done()

        remove_task.cancel()
        await asyncio.sleep(0)
        assert restore_store._lock.locked()
        assert not remove_task.done()
        assert not set_task.done()

        restore_store._store._async_write_data = original_async_write_data
        finish_delete.set()
        with pytest.raises(asyncio.CancelledError):
            await remove_task
        await set_task
    finally:
        finish_delete.set()
        await asyncio.gather(
            remove_task,
            *(task for task in (set_task,) if task is not None),
            return_exceptions=True,
        )

    assert (
        await IlluminatorRestoreStore(hass, "entry-cancelled-write").async_get(0, 1)
        == "InfraredMode"
    )


async def test_stale_restore_mode_is_reconciled_when_camera_left_white_mode():
    scheme, _lighting = _tables()
    rpc2 = SimpleNamespace(
        get_config=AsyncMock(return_value={"table": scheme}),
    )
    restore_store = _RestoreStore({(0, 1): "InfraredMode"})
    client = _client(rpc2, restore_store)

    await client.async_reconcile_lighting_scheme_restore_modes()

    assert restore_store.modes == {}


async def test_stale_poll_snapshot_cannot_delete_new_restore_mode():
    scheme, _lighting = _tables()
    scheme[0][1]["LightingMode"] = "WhiteMode"
    rpc2 = SimpleNamespace(
        get_config=AsyncMock(return_value={"table": scheme}),
    )
    restore_store = _RestoreStore({(0, 1): "AIMode"})
    client = _client(rpc2, restore_store)

    # Reconciliation was triggered by an earlier non-WhiteMode poll snapshot,
    # but re-reads the camera after taking the same lock as illuminator control.
    await client.async_reconcile_lighting_scheme_restore_modes()

    assert restore_store.modes == {(0, 1): "AIMode"}


async def test_missing_restore_mode_warns_only_after_successful_off(
    monkeypatch, caplog
):
    scheme, lighting = _tables()
    scheme[0][1]["LightingMode"] = "WhiteMode"
    rpc2 = SimpleNamespace(
        get_config=AsyncMock(side_effect=[{"table": scheme}, {"table": lighting}]),
        set_configs=AsyncMock(return_value={"result": True}),
    )
    client = _client(rpc2)
    monkeypatch.setattr(
        "custom_components.dahua.client.clear_host_cache", lambda _: None
    )

    with caplog.at_level(logging.WARNING):
        await client.async_set_lighting_scheme_illuminator(0, False, 70, 1, 1)

    assert "no saved recovery state exists" in caplog.text


async def test_missing_restore_mode_does_not_warn_when_off_write_fails(
    monkeypatch, caplog
):
    scheme, lighting = _tables()
    scheme[0][1]["LightingMode"] = "WhiteMode"
    rpc2 = SimpleNamespace(
        get_config=AsyncMock(side_effect=[{"table": scheme}, {"table": lighting}]),
        set_configs=AsyncMock(side_effect=ConnectionError("write failed")),
    )
    client = _client(rpc2)
    monkeypatch.setattr(
        "custom_components.dahua.client.clear_host_cache", lambda _: None
    )

    with caplog.at_level(logging.WARNING), pytest.raises(ConnectionError):
        await client.async_set_lighting_scheme_illuminator(0, False, 70, 1, 1)

    assert "no saved recovery state exists" not in caplog.text


async def test_lighting_scheme_reads_force_cgi():
    client = object.__new__(DahuaClient)
    client._request = AsyncMock(
        return_value={"table.LightingScheme[0][0].LightingMode": "AIMode"}
    )

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
    rpc2.request = AsyncMock(
        return_value={
            "result": True,
            "params": [{"result": True}, {"result": True}],
        }
    )
    scheme, lighting = _tables()

    await rpc2.set_configs(
        [
            ("LightingScheme", scheme),
            ("Lighting_V2", lighting),
        ]
    )

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
    rpc2.request = AsyncMock(
        return_value={
            "result": True,
            "params": [{"result": True}, {"result": False}],
        }
    )
    scheme, lighting = _tables()

    with pytest.raises(ConnectionError):
        await rpc2.set_configs(
            [
                ("LightingScheme", scheme),
                ("Lighting_V2", lighting),
            ]
        )


@pytest.mark.parametrize(
    "params",
    [
        None,
        [],
        [{"result": True}],
        [{"result": True}, {}],
        [{"result": True}, "OK"],
    ],
)
async def test_rpc2_multicall_requires_confirmation_for_every_write(params):
    rpc2 = object.__new__(DahuaRpc2Client)
    rpc2._id = 0
    rpc2._session_id = "session-1"
    rpc2.request = AsyncMock(return_value={"result": True, "params": params})
    scheme, lighting = _tables()

    with pytest.raises(ConnectionError):
        await rpc2.set_configs(
            [
                ("LightingScheme", scheme),
                ("Lighting_V2", lighting),
            ]
        )


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
