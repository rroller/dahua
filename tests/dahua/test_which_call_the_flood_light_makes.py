"""Which of three different calls the flood light makes, and why it matters.

`FloodLight.async_turn_on` and `async_turn_off` each branch twice, so one switch in
Home Assistant reaches the device in one of three ways:

    _supports_floodlightmode and a direct camera  ->  coaxial control
    _supports_floodlightmode and an NVR channel   ->  NVR coaxial control
    no floodlightmode support                     ->  Lighting_V2 flood light write

Only the first was exercised. That is the awkward one to be covering alone, because
the other two are the paths where getting it wrong is silent: an NVR channel sends
the *channel number* rather than the channel index, and a camera without flood light
mode never touches coaxial control at all. A light that writes to the wrong channel
turns on someone else's camera, and nothing in the log says so.

All three are driven here, so the file holds the whole table rather than two thirds of
it, and the calls are recorded rather than loosely mocked so a branch that quietly
does two things fails. `test_flood_light_mode.py` covers the separate question of the
mode being written as a number rather than as the table it was parsed from.
"""

from types import SimpleNamespace

import pytest

from custom_components.dahua.client import SECURITY_LIGHT_TYPE
from custom_components.dahua.light import FloodLight

CHANNEL_INDEX = 3
CHANNEL_NUMBER = 4
PROFILE_MODE = "1"


class _Client:
    """Records every write, so a test can assert what was *not* called too."""

    def __init__(self):
        self.modes_set = []
        self.coaxial = []
        self.nvr_coaxial = []
        self.lighting_v2 = []

    async def async_get_floodlightmode(self):
        return 1

    async def async_set_floodlightmode(self, mode):
        self.modes_set.append(mode)

    async def async_set_coaxial_control_state(self, channel, dahua_type, enabled):
        self.coaxial.append((channel, dahua_type, enabled))

    async def async_set_nvr_coaxial_control_state(self, channel, dahua_type, enabled):
        self.nvr_coaxial.append((channel, dahua_type, enabled))

    async def async_set_lighting_v2_for_flood_lights(self, channel, enabled, profile_mode):
        self.lighting_v2.append((channel, enabled, profile_mode))


async def _never():
    return None


def _flood_light(*, supports_floodlightmode, is_nvr_channel):
    coordinator = SimpleNamespace(
        client=_Client(),
        _supports_floodlightmode=supports_floodlightmode,
        _floodlight_mode=2,
        get_channel=lambda: CHANNEL_INDEX,
        get_channel_number=lambda: CHANNEL_NUMBER,
        get_profile_mode=lambda: PROFILE_MODE,
        is_nvr_channel=lambda: is_nvr_channel,
        async_refresh=_never,
    )
    entity = object.__new__(FloodLight)
    entity._coordinator = coordinator
    return entity, coordinator


# --- a direct camera goes straight at the camera -----------------------------


@pytest.mark.parametrize("turning_on", [True, False])
async def test_a_direct_camera_uses_the_direct_call(turning_on):
    """The third branch, here so this file holds the whole table rather than two
    thirds of it. It also pins the distinction the recorder test depends on: the
    direct call takes the channel *index* where the recorder call takes the
    channel *number*.
    """
    entity, coordinator = _flood_light(
        supports_floodlightmode=True, is_nvr_channel=False)

    await (entity.async_turn_on() if turning_on else entity.async_turn_off())

    client = coordinator.client
    assert client.coaxial == [
        (CHANNEL_INDEX, SECURITY_LIGHT_TYPE, turning_on)], client.coaxial
    assert client.nvr_coaxial == [], "a direct camera must not use the recorder call"


# --- an NVR channel goes through the recorder --------------------------------


@pytest.mark.parametrize("turning_on", [True, False])
async def test_an_nvr_channel_uses_the_recorder_call(turning_on):
    entity, coordinator = _flood_light(
        supports_floodlightmode=True, is_nvr_channel=True)

    await (entity.async_turn_on() if turning_on else entity.async_turn_off())

    client = coordinator.client
    assert client.nvr_coaxial == [
        (CHANNEL_NUMBER, SECURITY_LIGHT_TYPE, turning_on)], client.nvr_coaxial
    assert client.coaxial == [], "a recorder channel must not use the direct call"


@pytest.mark.parametrize("turning_on", [True, False])
async def test_the_recorder_call_is_given_the_channel_number(turning_on):
    """The one that would be invisible. `get_channel()` is the zero based index and
    `get_channel_number()` is what the recorder calls the channel, and they differ by
    one: sending the index turns on the light of the camera next door."""
    entity, coordinator = _flood_light(
        supports_floodlightmode=True, is_nvr_channel=True)

    await (entity.async_turn_on() if turning_on else entity.async_turn_off())

    sent = coordinator.client.nvr_coaxial[0][0]
    assert sent == CHANNEL_NUMBER, "sent the channel index, not the channel number"
    assert sent != CHANNEL_INDEX, "the fixture must keep these two apart to prove it"


# --- and a camera without flood light mode writes the table instead ----------


@pytest.mark.parametrize("turning_on", [True, False])
async def test_without_floodlightmode_it_writes_lighting_v2(turning_on):
    entity, coordinator = _flood_light(
        supports_floodlightmode=False, is_nvr_channel=False)

    await (entity.async_turn_on() if turning_on else entity.async_turn_off())

    client = coordinator.client
    assert client.lighting_v2 == [
        (CHANNEL_INDEX, turning_on, PROFILE_MODE)], client.lighting_v2


@pytest.mark.parametrize("turning_on", [True, False])
async def test_without_floodlightmode_nothing_coaxial_is_touched(turning_on):
    """Coaxial control is undocumented and model dependent, and on some devices a
    write to it is not reversible. A camera that does not report flood light mode
    must not have it poked speculatively."""
    entity, coordinator = _flood_light(
        supports_floodlightmode=False, is_nvr_channel=False)

    await (entity.async_turn_on() if turning_on else entity.async_turn_off())

    client = coordinator.client
    assert client.coaxial == []
    assert client.nvr_coaxial == []
    assert client.modes_set == [], "the mode is not written on this path"


async def test_the_lighting_v2_path_is_taken_even_on_a_recorder():
    """`is_nvr_channel` is only consulted inside the floodlightmode branch, so a
    recorder channel whose camera does not report the mode still writes the table.
    Reading the two branches as independent is the easy mistake here."""
    entity, coordinator = _flood_light(
        supports_floodlightmode=False, is_nvr_channel=True)

    await entity.async_turn_on()

    assert coordinator.client.lighting_v2 == [(CHANNEL_INDEX, True, PROFILE_MODE)]
    assert coordinator.client.nvr_coaxial == []
