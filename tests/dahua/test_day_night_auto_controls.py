"""Day/Night Auto-only sensitivity and switch-delay controls."""

import pytest

from custom_components.dahua.const import (
    DAY_NIGHT_SENSITIVITY_OPTION_BY_VALUE,
    DAY_NIGHT_SENSITIVITY_VALUE_BY_OPTION,
    DAY_NIGHT_SWITCH_DELAY_OPTIONS,
)
from custom_components.dahua.coordinator import (
    DahuaDataUpdateCoordinator,
    day_night_sensitivity_option,
    day_night_switch_delay,
)

CHANNEL = 3
MODE_KEY = f"table.VideoInOptions[{CHANNEL}].DayNightColor"
SENSITIVITY_KEY = f"table.VideoInOptions[{CHANNEL}].DayNightSensitivity"
DELAY_KEY = f"table.VideoInOptions[{CHANNEL}].DayNightSwitchDelay"


def test_mappings_and_range():
    assert DAY_NIGHT_SENSITIVITY_VALUE_BY_OPTION == {"high": 1, "medium": 2, "low": 3}
    assert DAY_NIGHT_SENSITIVITY_OPTION_BY_VALUE == {1: "high", 2: "medium", 3: "low"}
    assert DAY_NIGHT_SWITCH_DELAY_OPTIONS == tuple(str(value) for value in range(2, 31))


def test_exact_channel_and_fail_closed_reads():
    assert day_night_sensitivity_option({SENSITIVITY_KEY: "2"}, CHANNEL) == "medium"
    assert (
        day_night_sensitivity_option(
            {"table.VideoInOptions[0].DayNightSensitivity": "2"}, CHANNEL
        )
        is None
    )
    assert day_night_switch_delay({DELAY_KEY: "6"}, CHANNEL) == 6
    assert day_night_switch_delay({DELAY_KEY: "31"}, CHANNEL) is None


class Client:
    def __init__(self, before, after):
        self.before, self.after, self.reads, self.writes = before, after, 0, []

    async def async_get_video_in_options(self, fresh=False):
        assert fresh is True
        self.reads += 1
        return self.before if self.reads == 1 else self.after

    async def async_set_video_in_option(self, channel, field, value):
        self.writes.append((channel, field, value))


def coordinator(before, after):
    value = object.__new__(DahuaDataUpdateCoordinator)
    value._channel = CHANNEL
    value._supports_day_night_sensitivity = True
    value._supports_day_night_switch_delay = True
    value.client = Client(before, after)
    return value


async def test_sensitivity_fresh_read_write_readback():
    c = coordinator(
        {MODE_KEY: "1", SENSITIVITY_KEY: "2"}, {MODE_KEY: "1", SENSITIVITY_KEY: "1"}
    )
    await c.async_set_day_night_sensitivity("high")
    assert c.client.writes == [(CHANNEL, "DayNightSensitivity", 1)]
    assert c.client.reads == 2


async def test_delay_fresh_read_write_readback():
    c = coordinator({MODE_KEY: "1", DELAY_KEY: "5"}, {MODE_KEY: "1", DELAY_KEY: "6"})
    await c.async_set_day_night_switch_delay(6)
    assert c.client.writes == [(CHANNEL, "DayNightSwitchDelay", 6)]
    assert c.client.reads == 2


@pytest.mark.parametrize("mode", ["0", "2", None, "9"])
async def test_non_auto_fails_closed_without_write(mode):
    c = coordinator(
        {MODE_KEY: mode, SENSITIVITY_KEY: "2"}, {MODE_KEY: mode, SENSITIVITY_KEY: "2"}
    )
    with pytest.raises(ValueError, match="must be Auto"):
        await c.async_set_day_night_sensitivity("high")
    assert c.client.writes == []
    assert c.client.reads == 1


async def test_readback_mismatch_is_failure():
    c = coordinator({MODE_KEY: "1", DELAY_KEY: "5"}, {MODE_KEY: "1", DELAY_KEY: "5"})
    with pytest.raises(RuntimeError, match="did not apply"):
        await c.async_set_day_night_switch_delay(6)
