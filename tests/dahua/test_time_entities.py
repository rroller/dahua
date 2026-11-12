"""Profile Schedule time entities preserve camera state and context."""

from datetime import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.dahua.profile import ProfileScheduleTimes
from custom_components.dahua.time import (
    DahuaProfileDayStartTime,
    DahuaProfileNightStartTime,
)


def _entity(endpoint="day_start", mode="schedule"):
    coordinator = SimpleNamespace(
        get_serial_number=lambda: "SER1",
        get_profile_management_mode=lambda: mode,
        get_profile_schedule_times=lambda: ProfileScheduleTimes(
            time(8, 23),
            time(17, 47),
        ),
        async_set_profile_schedule_times=AsyncMock(),
        async_refresh=AsyncMock(),
    )

    entity_class = (
        DahuaProfileDayStartTime
        if endpoint == "day_start"
        else DahuaProfileNightStartTime
    )

    entity = object.__new__(entity_class)
    entity._coordinator = coordinator

    return entity, coordinator


def test_day_and_night_values_stay_visible_from_camera_state():
    day, _ = _entity(
        "day_start",
        mode="normal",
    )
    night, _ = _entity(
        "night_start",
        mode="normal",
    )

    assert day.native_value == time(8, 23)
    assert night.native_value == time(17, 47)


async def test_one_endpoint_write_preserves_the_peer():
    entity, coordinator = _entity("day_start")

    await entity.async_set_value(time(9, 11))

    coordinator.async_set_profile_schedule_times.assert_awaited_once_with(
        day_start=time(9, 11),
    )
    coordinator.async_refresh.assert_awaited_once()


async def test_write_outside_schedule_is_rejected_before_camera_io():
    entity, coordinator = _entity(mode="normal")

    with pytest.raises(HomeAssistantError) as error:
        await entity.async_set_value(time(9, 11))

    assert error.value.translation_key == "profile_schedule_requires_schedule_mode"

    coordinator.async_set_profile_schedule_times.assert_not_awaited()
    coordinator.async_refresh.assert_not_awaited()
