"""Schedule regressions reached through real entity and client methods."""

from copy import deepcopy
from datetime import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from custom_components.dahua.client import DahuaClient
from custom_components.dahua import coordinator as coordinator_module
from custom_components.dahua.coordinator import DahuaDataUpdateCoordinator
from custom_components.dahua.profile import (
    ProfileScheduleTimes,
    profile_schedule_table_with_times,
    resolve_profile_schedule_table,
    resolve_profile_schedule_times,
)
from custom_components.dahua.time import (
    DahuaProfileDayStartTime,
    DahuaProfileNightStartTime,
)
from tests.dahua.test_what_setup_probes import _coordinator


def _table(interval="1 07:00:00-19:00:00"):
    return [{"Mode": 1, "Config": [0, 1], "TimeSection": [[interval]]}]


def test_both_entities_have_distinct_channel_owned_unique_ids():
    coordinator = SimpleNamespace(get_serial_number=lambda: "RECORDER_3")
    day = DahuaProfileDayStartTime(coordinator, object())
    night = DahuaProfileNightStartTime(coordinator, object())
    assert day.unique_id == "RECORDER_3_profile_day_start"
    assert night.unique_id == "RECORDER_3_profile_night_start"


@pytest.mark.parametrize("endpoint", ["day_start", "night_start"])
@pytest.mark.parametrize("channel", [0, 1])
async def test_one_endpoint_keeps_the_fresh_camera_peer_not_the_cached_one(
    endpoint, channel
):
    table = _table()
    if channel:
        table.insert(0, {"Mode": 0, "Config": [2], "OtherChannel": True})

    async def read(params):
        assert params == {"name": "VideoInMode"}
        return {"table": deepcopy(table)}

    async def write(name, wanted, *, channel):
        assert name == "VideoInMode"
        assert wanted[channel]["Mode"] == 1
        table[:] = deepcopy(wanted)

    rpc = SimpleNamespace(get_config=read, set_config=write)
    client = object.__new__(DahuaClient)
    client._device = "schedule-review-camera"

    async def call(callback):
        return await callback(rpc)

    client._rpc2_shared_call = call
    coordinator = SimpleNamespace(
        get_profile_management_mode=lambda: "schedule",
        get_profile_schedule_times=lambda: ProfileScheduleTimes(time(6), time(18)),
        async_refresh=AsyncMock(),
    )

    async def set_times(*args, **kwargs):
        return await client.async_set_profile_schedule_times(channel, *args, **kwargs)

    coordinator.async_set_profile_schedule_times = set_times
    cls = (
        DahuaProfileDayStartTime
        if endpoint == "day_start"
        else DahuaProfileNightStartTime
    )
    entity = object.__new__(cls)
    entity._coordinator = coordinator
    await entity.async_set_value(time(9) if endpoint == "day_start" else time(21))
    expected = (
        "1 09:00:00-19:00:00" if endpoint == "day_start" else "1 07:00:00-21:00:00"
    )
    assert table[channel]["TimeSection"][0][0] == expected
    if channel:
        assert table[0] == {"Mode": 0, "Config": [2], "OtherChannel": True}


@pytest.mark.parametrize("other", ["2 08:00:00-20:00:00", "1 invalid", None])
def test_unknown_or_malformed_neighbor_cannot_authorize_a_schedule_write(other):
    table = _table()
    table[0]["TimeSection"][0].append(other)
    flat = {
        "table.VideoInMode[0].TimeSection[0][0]": table[0]["TimeSection"][0][0],
        "table.VideoInMode[0].TimeSection[0][1]": other,
    }
    assert resolve_profile_schedule_times(flat, 0) is None
    assert resolve_profile_schedule_table(table, 0) is None
    with pytest.raises(ConnectionError, match="unambiguous"):
        profile_schedule_table_with_times(table, 0, time(8), time(20))


async def test_time_only_setup_discovers_the_schedule_without_exposure_probes(hass):
    coordinator = _coordinator(hass)
    coordinator._wanted_by = lambda *platforms: "time" in platforms
    coordinator.client.async_get_video_in_mode = AsyncMock(
        return_value={
            "table.VideoInMode[0].Mode": "1",
            "table.VideoInMode[0].Config[0]": "0",
            "table.VideoInMode[0].Config[1]": "1",
            "table.VideoInMode[0].TimeSection[0][0]": "1 07:00:00-19:00:00",
        }
    )
    await coordinator._async_update_data()
    assert coordinator.initialized
    assert coordinator.supports_profile_schedule()
    assert "async_get_exposure_anti_flicker_config" not in coordinator.client.asked


async def test_time_only_timeout_can_discover_entities_on_a_later_poll(
    hass, monkeypatch
):
    coordinator = _coordinator(hass)
    coordinator._note_probe_refusal = (
        DahuaDataUpdateCoordinator._note_probe_refusal.__get__(coordinator)
    )
    coordinator.client.async_get_video_in_mode = AsyncMock(
        side_effect=[
            TimeoutError(),
            {"table.VideoInMode[0].TimeSection[0][0]": "1 07:00:00-19:00:00"},
        ]
    )
    assert not await coordinator._async_probe_profile_schedule()
    assert "profile_schedule" in coordinator.probes_that_did_not_answer()
    notify = Mock()
    monkeypatch.setattr(
        coordinator_module, "async_notify_capability_discovered", notify
    )
    await coordinator._async_retry_unanswered_capabilities()
    assert coordinator.supports_profile_schedule()
    notify.assert_called_once_with(hass, "e1", coordinator.get_serial_number())


async def test_disabled_time_and_exposure_platforms_do_not_probe_schedule(hass):
    coordinator = _coordinator(hass)
    coordinator._wanted_by = lambda *platforms: False
    coordinator.client.async_get_video_in_mode = AsyncMock()
    await coordinator._async_update_data()
    assert coordinator.initialized
    coordinator.client.async_get_video_in_mode.assert_not_awaited()
