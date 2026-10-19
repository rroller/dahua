"""An unreadable relay state must never be reported as physically off."""

from unittest.mock import AsyncMock

import pytest

from custom_components.dahua.client import DahuaClient
from custom_components.dahua.coordinator import DahuaDataUpdateCoordinator


@pytest.mark.parametrize("answer", [{}, {"result": "Error"}, {"result": "-1"}])
async def test_unreadable_relay_states_have_no_known_bits(answer):
    client = DahuaClient("u", "p", "cam", 80, 554, object())
    client.get = AsyncMock(return_value=answer)
    assert await client.async_get_alarm_output_state(6) == {}


@pytest.mark.parametrize(
    "value,expected", [(None, None), ("garbage", None), ("0", False), ("1", True)]
)
def test_relay_state_distinguishes_unknown_from_open(value, expected):
    coordinator = object.__new__(DahuaDataUpdateCoordinator)
    coordinator.data = {"status.AlarmOut[5]": value}
    assert coordinator.is_alarm_output_on(5) is expected
    assert coordinator.is_alarm_output_on(0) is None
