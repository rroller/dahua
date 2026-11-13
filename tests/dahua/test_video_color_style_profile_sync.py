"""Profile-aware, two-way VideoColor Style reads and writes."""

import pytest

from custom_components.dahua.client import DahuaClient
from custom_components.dahua.const import (
    VIDEO_COLOR_STYLE_OPTION_BY_VALUE,
    VIDEO_COLOR_STYLE_OPTIONS,
    VIDEO_COLOR_STYLE_VALUE_BY_OPTION,
)
from custom_components.dahua.coordinator import (
    DahuaDataUpdateCoordinator,
    video_color_style_option,
    video_color_style_supported,
)

CHANNEL = 3
PROFILE_KEY = f"table.VideoInMode[{CHANNEL}].Config[0]"
CONFIG_EX_KEY = f"table.VideoInMode[{CHANNEL}].ConfigEx"


def _style_key(channel=CHANNEL, profile=0):
    return f"table.VideoColor[{channel}][{profile}].Style"


def test_ui_options_map_to_measured_dahua_wire_values():
    assert VIDEO_COLOR_STYLE_OPTIONS == ("Soft", "Standard", "Vivid")
    assert VIDEO_COLOR_STYLE_VALUE_BY_OPTION == {
        "Soft": "Gentle",
        "Standard": "Standard",
        "Vivid": "Flamboyant",
    }
    assert VIDEO_COLOR_STYLE_OPTION_BY_VALUE == {
        "Gentle": "Soft",
        "Standard": "Standard",
        "Flamboyant": "Vivid",
    }


@pytest.mark.parametrize(
    ("profile", "wire", "option"),
    [
        (0, "Flamboyant", "Vivid"),
        (1, "Gentle", "Soft"),
        (2, "Standard", "Standard"),
    ],
)
def test_style_reads_the_exact_active_profile(profile, wire, option):
    data = {_style_key(profile=profile): wire}
    assert video_color_style_option(data, CHANNEL, profile) == option


def test_style_never_falls_back_to_another_channel_or_profile():
    data = {
        _style_key(channel=0, profile=1): "Gentle",
        _style_key(profile=0): "Flamboyant",
        _style_key(profile=2): "Standard",
    }
    assert video_color_style_option(data, CHANNEL, 1) is None


@pytest.mark.parametrize("wire", ["Soft", "Vivid", "Custom", None, 1])
def test_unknown_wire_style_is_not_guessed(wire):
    data = {_style_key(profile=1): wire}
    assert video_color_style_option(data, CHANNEL, 1) is None


def test_capability_accepts_recognized_style_on_any_profile_of_this_channel():
    assert video_color_style_supported({_style_key(profile=1): "Gentle"}, CHANNEL)
    other = {_style_key(channel=0, profile=1): "Gentle"}
    assert not video_color_style_supported(other, CHANNEL)


def test_same_entity_tracks_day_night_and_general_profile_changes():
    coordinator = object.__new__(DahuaDataUpdateCoordinator)
    coordinator._channel = CHANNEL
    coordinator.data = {
        PROFILE_KEY: "0",
        CONFIG_EX_KEY: "Day",
        _style_key(profile=0): "Flamboyant",
        _style_key(profile=1): "Gentle",
        _style_key(profile=2): "Standard",
    }
    assert coordinator.get_video_color_style() == "Vivid"
    coordinator.data = dict(coordinator.data, **{CONFIG_EX_KEY: "Night"})
    assert coordinator.get_video_color_style() == "Soft"
    coordinator.data = dict(coordinator.data, **{PROFILE_KEY: "2"})
    assert coordinator.get_video_color_style() == "Standard"


class _WriteClient:
    def __init__(self, mode, colors, readback=None):
        self.mode = mode
        self.colors = colors
        self.readback = colors if readback is None else readback
        self.calls = []
        self._color_reads = 0

    async def async_get_video_in_mode(self, fresh=False):
        self.calls.append(("VideoInMode", fresh))
        return self.mode

    async def async_get_video_color(self, fresh=False):
        self.calls.append(("VideoColor", fresh))
        self._color_reads += 1
        return self.colors if self._color_reads == 1 else self.readback

    async def async_set_video_color_style(self, channel, profile, value):
        self.calls.append(("setConfig", channel, profile, value))


def _write_coordinator(mode, colors, readback=None):
    coordinator = object.__new__(DahuaDataUpdateCoordinator)
    coordinator._channel = CHANNEL
    coordinator.data = {PROFILE_KEY: "0", CONFIG_EX_KEY: "Day"}
    coordinator.client = _WriteClient(mode, colors, readback)
    return coordinator


async def test_write_uses_fresh_profile_maps_value_and_verifies_readback():
    key = _style_key(profile=1)
    coordinator = _write_coordinator(
        {PROFILE_KEY: "0", CONFIG_EX_KEY: "Night"},
        {key: "Standard"},
        {key: "Flamboyant"},
    )
    await coordinator.async_set_video_color_style("Vivid")
    assert coordinator.client.calls == [
        ("VideoInMode", True),
        ("VideoColor", True),
        ("setConfig", CHANNEL, 1, "Flamboyant"),
        ("VideoColor", True),
    ]


async def test_write_requires_the_exact_current_profile_field():
    coordinator = _write_coordinator(
        {PROFILE_KEY: "0", CONFIG_EX_KEY: "Night"},
        {_style_key(profile=0): "Standard"},
    )
    with pytest.raises(ConnectionError, match="could not be established"):
        await coordinator.async_set_video_color_style("Soft")
    assert coordinator.client.calls == [
        ("VideoInMode", True),
        ("VideoColor", True),
    ]


async def test_acknowledged_write_with_wrong_readback_fails_closed():
    key = _style_key(profile=1)
    coordinator = _write_coordinator(
        {PROFILE_KEY: "0", CONFIG_EX_KEY: "Night"},
        {key: "Standard"},
        {key: "Standard"},
    )
    with pytest.raises(ConnectionError, match="did not apply"):
        await coordinator.async_set_video_color_style("Vivid")


async def test_client_writes_only_the_exact_profile_style_field():
    client = object.__new__(DahuaClient)
    calls = []

    async def get(url):
        calls.append(url)
        return "OK"

    client.get = get
    await client.async_set_video_color_style(CHANNEL, 2, "Gentle")
    assert calls == [
        "/cgi-bin/configManager.cgi?action=setConfig&VideoColor[3][2].Style=Gentle"
    ]


@pytest.mark.parametrize(
    "arguments",
    [
        (-1, 0, "Gentle"),
        (CHANNEL, -1, "Gentle"),
        (CHANNEL, 0, "Soft"),
        (CHANNEL, 0, "Custom"),
    ],
)
async def test_client_rejects_invalid_style_addresses_or_wire_values(arguments):
    client = object.__new__(DahuaClient)
    client.get = pytest.fail
    with pytest.raises(ValueError):
        await client.async_set_video_color_style(*arguments)
