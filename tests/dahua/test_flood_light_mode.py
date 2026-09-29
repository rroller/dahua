"""What the flood light writes back must be the mode, not the table it came from.

`async_get_floodlightmode` returned the parsed `key=value` dict from
`configManager.cgi`, and `light.py` stored that dict and handed it to
`async_set_floodlightmode`, which formats its argument straight into the URL.
Turning the flood light off therefore sent

    FloodLightMode.Mode={'FloodLightMode.Mode': '1'}

which the device refuses or ignores, leaving the camera in manual mode for
good. The method's own error path already returned the int 2, so the return
type was half int; it is now wholly the mode number.
"""

from types import SimpleNamespace

import pytest

from custom_components.dahua.client import DahuaClient
from custom_components.dahua.light import FloodLight


async def _mode(payload):
    client = object.__new__(DahuaClient)

    async def async_get_config(name):
        assert name == "FloodLightMode.Mode"
        return payload

    client.async_get_config = async_get_config
    return await client.async_get_floodlightmode()


# --- the client returns the number -------------------------------------------

@pytest.mark.parametrize("payload,expected", [
    ({"FloodLightMode.Mode": "1"}, 1),
    ({"table.FloodLightMode[0].Mode": "3"}, 3),
    ({"table.FloodLightMode[0].Mode": "4", "table.Other": "x"}, 4),
    ({"FloodLightMode.Mode": " 2 "}, 2),
    ("1", 1),
])
async def test_the_mode_is_pulled_out_of_the_answer(payload, expected):
    assert await _mode(payload) == expected


@pytest.mark.parametrize("payload", [{}, None, {"FloodLightMode.Mode": "bad"},
                                     {"a": "1", "b": "2"}])
async def test_an_unusable_answer_is_manual(payload):
    """Manual is what the light entity itself writes while it is on, so it is
    the least surprising fallback."""
    assert await _mode(payload) == 2


# --- and the light hands the number back, not the dict ------------------------

class _Client:
    def __init__(self):
        self.modes_set = []
        self.coaxial = []

    async def async_get_floodlightmode(self):
        return 1

    async def async_set_floodlightmode(self, mode):
        self.modes_set.append(mode)

    async def async_set_coaxial_control_state(self, channel, dahua_type, enabled):
        self.coaxial.append((channel, dahua_type, enabled))


def _flood_light():
    coordinator = SimpleNamespace(
        client=_Client(),
        _supports_floodlightmode=True,
        _floodlight_mode=2,
        get_channel=lambda: 0,
        is_nvr_channel=lambda: False,
        async_refresh=_never,
    )
    entity = object.__new__(FloodLight)
    entity._coordinator = coordinator
    return entity, coordinator


async def _never():
    return None


async def test_turning_off_sends_the_stored_mode_not_the_table():
    entity, coordinator = _flood_light()

    await entity.async_turn_on()
    assert coordinator.client.modes_set == [2], "turning on switches to manual"
    assert coordinator._floodlight_mode == 1, "the previous mode is remembered"

    await entity.async_turn_off()
    assert coordinator.client.modes_set == [2, 1]
    assert coordinator.client.modes_set[-1] == 1, (
        "the mode is written as a number; a dict here goes into the URL")
