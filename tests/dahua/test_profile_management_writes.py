"""Exercise actual client writes and delayed discovery, beyond pure table helpers."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from custom_components.dahua import coordinator as coordinator_module
from custom_components.dahua.client import DahuaClient, _HOST_VIDEO_IN_MODE_LOCKS
from custom_components.dahua.coordinator import DahuaDataUpdateCoordinator
from tests.dahua.test_poll_skips_unused import _coordinator


@pytest.fixture(autouse=True)
def clear_video_in_mode_locks():
    _HOST_VIDEO_IN_MODE_LOCKS.clear()
    yield
    _HOST_VIDEO_IN_MODE_LOCKS.clear()


def _client(rpc):
    client = DahuaClient("u", "p", "profile-camera", 80, 554, object())

    async def call(operation):
        return await operation(rpc)

    client._rpc2_shared_call = call
    return client


def _table():
    return [
        {"Mode": 0, "Config": [0], "TimeSection": [["keep"]]},
        {"Mode": 0, "Config": [2], "FutureField": {"preserve": 7}},
    ]


async def test_actual_writer_preserves_the_other_channel_and_unknown_fields():
    before = _table()
    wanted = deepcopy(before)
    wanted[1].update(Mode=1, Config=[0, 1])
    rpc = SimpleNamespace(
        get_config=AsyncMock(side_effect=[{"table": before}, {"table": wanted}]),
        set_config=AsyncMock(),
    )
    assert await _client(rpc).async_set_profile_management(1, "schedule") == "schedule"
    rpc.set_config.assert_awaited_once_with("VideoInMode", wanted, channel=1)
    assert rpc.get_config.await_args_list[0].args == ({"name": "VideoInMode"},)
    assert rpc.get_config.await_count == 2
    assert before == _table()


async def test_an_acknowledged_write_to_the_wrong_channel_is_not_success():
    before = _table()
    wrong = deepcopy(before)
    wrong[0].update(Mode=1, Config=[0, 1])
    rpc = SimpleNamespace(
        get_config=AsyncMock(side_effect=[{"table": before}, {"table": wrong}]),
        set_config=AsyncMock(),
    )
    with pytest.raises(ConnectionError, match="read back 'normal'"):
        await _client(rpc).async_set_profile_management(1, "schedule")


async def test_missing_channel_never_writes_the_first_row():
    rpc = SimpleNamespace(
        get_config=AsyncMock(return_value={"table": _table()}),
        set_config=AsyncMock(),
    )
    with pytest.raises(ConnectionError, match="no channel 3"):
        await _client(rpc).async_set_profile_management(3, "schedule")
    rpc.set_config.assert_not_awaited()


async def test_legacy_day_night_write_cannot_be_lost_behind_a_table_write():
    table = _table()
    snapshot_taken = asyncio.Event()
    release_table_write = asyncio.Event()
    first_read = True

    async def read(params):
        nonlocal first_read
        assert params == {"name": "VideoInMode"}
        snapshot = deepcopy(table)
        if first_read:
            first_read = False
            snapshot_taken.set()
            await release_table_write.wait()
        return {"table": snapshot}

    async def write(name, wanted, *, channel):
        assert (name, channel) == ("VideoInMode", 1)
        table[:] = deepcopy(wanted)

    async def cgi(url, *args):
        assert url == (
            "/cgi-bin/configManager.cgi?action=setConfig&VideoInMode[0].Config[0]=1"
        )
        table[0]["Config"][0] = 1
        return {"OK": ""}

    rpc = SimpleNamespace(get_config=read, set_config=write)
    management = _client(rpc)
    legacy = _client(rpc)
    legacy.get = cgi
    task = asyncio.create_task(management.async_set_profile_management(1, "schedule"))
    await snapshot_taken.wait()
    other = asyncio.create_task(legacy.async_set_video_profile_mode(0, "Night"))
    for _ in range(10):
        await asyncio.sleep(0)
    release_table_write.set()
    await asyncio.gather(task, other)
    assert table[0]["Config"] == [1], "the successful legacy write must survive"
    assert table[1]["Config"] == [0, 1]


@pytest.mark.parametrize(
    "name", ["exposure_anti_flicker", "exposure_mode", "exposure_compensation"]
)
async def test_delayed_mode_discovery_notifies_even_without_exposure(name, monkeypatch):
    coordinator = object.__new__(DahuaDataUpdateCoordinator)
    coordinator._address = "profile-camera"
    coordinator._channel = 1
    coordinator.hass = object()
    coordinator.config_entry = SimpleNamespace(entry_id="profile-entry")
    coordinator.get_serial_number = lambda: "camera-channel-1"
    coordinator._notify_late_work_settled = lambda: None
    coordinator._note_probe_refusal(name, TimeoutError())
    coordinator.client = SimpleNamespace(
        async_get_video_in_mode=AsyncMock(
            return_value={
                "table.VideoInMode[1].Mode": "0",
                "table.VideoInMode[1].Config[0]": "2",
            }
        ),
        async_get_exposure_anti_flicker_config=AsyncMock(return_value={}),
    )
    notify = Mock()
    monkeypatch.setattr(
        coordinator_module, "async_notify_capability_discovered", notify
    )
    await coordinator._async_retry_unanswered_capabilities()
    assert coordinator.supports_profile_management()
    notify.assert_called_once_with(
        coordinator.hass, "profile-entry", "camera-channel-1"
    )


async def test_poll_discovers_the_selector_when_the_mode_shape_becomes_supported(
    monkeypatch,
):
    coordinator = _coordinator(select=True)
    coordinator._supports_profile_mode = True
    coordinator.client.async_get_video_in_mode = AsyncMock(
        return_value={
            "table.VideoInMode[0].Mode": "0",
            "table.VideoInMode[0].Config[0]": "2",
        }
    )
    notify = Mock()
    monkeypatch.setattr(
        coordinator_module, "async_notify_capability_discovered", notify
    )
    await coordinator._async_update_data()
    assert coordinator.supports_profile_management()
    notify.assert_called_once_with(coordinator.hass, "e1", "SER1")
    await coordinator._async_update_data()
    assert notify.call_count == 1
