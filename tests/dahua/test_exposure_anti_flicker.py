"""Profile-aware Exposure Anti-Flicker reads and writes."""

import json
from pathlib import Path

import pytest

from custom_components.dahua.client import DahuaClient, clear_host_cache
from custom_components.dahua.const import (
    EXPOSURE_ANTI_FLICKER_OPTION_BY_VALUE,
    EXPOSURE_ANTI_FLICKER_OPTIONS,
    EXPOSURE_ANTI_FLICKER_VALUE_BY_OPTION,
)
from custom_components.dahua.coordinator import (
    DahuaDataUpdateCoordinator,
    active_profile_index,
    exposure_anti_flicker_option,
)
from custom_components.dahua.select import DahuaExposureAntiFlickerSelect

CHANNEL = 3
PROFILE_KEY = f"table.VideoInMode[{CHANNEL}].Config[0]"
PROFILE_CONFIG_EX_KEY = f"table.VideoInMode[{CHANNEL}].ConfigEx"


def _exposure_key(channel, profile):
    return f"table.VideoInExposure[{channel}][{profile}].AntiFlicker"


@pytest.mark.parametrize(("raw", "expected"), [("0", 0), (0, 0), ("1", 1), (2, 2)])
def test_active_profile_index_reads_this_channels_reported_value(raw, expected):
    assert active_profile_index({PROFILE_KEY: raw}, CHANNEL) == expected


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"table.VideoInMode[3].Config": "2"},
        {PROFILE_KEY: ""},
        {PROFILE_KEY: "not-an-index"},
        {PROFILE_KEY: True},
        {PROFILE_KEY: -1},
        {PROFILE_KEY: "-1"},
    ],
)
def test_missing_or_invalid_active_profile_is_not_guessed(data):
    assert active_profile_index(data, CHANNEL) is None


def test_active_profile_index_preserves_general_profile_precedence():
    assert (
        active_profile_index(
            {PROFILE_KEY: "2", PROFILE_CONFIG_EX_KEY: "Night"}, CHANNEL
        )
        == 2
    )


def test_active_profile_index_uses_config_ex_when_config_is_static_zero():
    assert (
        active_profile_index(
            {PROFILE_KEY: "0", PROFILE_CONFIG_EX_KEY: "Night"}, CHANNEL
        )
        == 1
    )


def test_unrecognized_profile_semantics_fail_closed():
    assert (
        active_profile_index(
            {PROFILE_KEY: "not-an-index", PROFILE_CONFIG_EX_KEY: "Auto"}, CHANNEL
        )
        is None
    )


@pytest.mark.parametrize(
    ("value", "option"),
    [(0, "outdoor"), ("1", "50_hz"), (2, "60_hz")],
)
def test_supported_antiflicker_values_map_to_select_options(value, option):
    assert (
        exposure_anti_flicker_option({_exposure_key(CHANNEL, 1): value}, CHANNEL, 1)
        == option
    )


@pytest.mark.parametrize("value", [True, "3", 3, "auto", None])
def test_unsupported_antiflicker_value_is_unknown(value):
    assert (
        exposure_anti_flicker_option({_exposure_key(CHANNEL, 1): value}, CHANNEL, 1)
        is None
    )


def test_antiflicker_lookup_never_falls_back_to_another_row_or_channel():
    data = {
        _exposure_key(CHANNEL, 0): "0",
        _exposure_key(CHANNEL, 2): "2",
        _exposure_key(0, 1): "1",
    }

    assert exposure_anti_flicker_option(data, CHANNEL, 1) is None


def test_state_tracks_the_profile_reported_on_each_update():
    coordinator = object.__new__(DahuaDataUpdateCoordinator)
    coordinator._channel = CHANNEL
    coordinator.data = {
        PROFILE_KEY: "0",
        _exposure_key(CHANNEL, 0): "0",
        _exposure_key(CHANNEL, 1): "1",
        _exposure_key(CHANNEL, 2): "2",
    }

    assert coordinator.get_exposure_anti_flicker() == "outdoor"

    coordinator.data = dict(coordinator.data, **{PROFILE_KEY: "1"})
    assert coordinator.get_exposure_anti_flicker() == "50_hz"

    coordinator.data = dict(coordinator.data, **{PROFILE_KEY: "2"})
    assert coordinator.get_exposure_anti_flicker() == "60_hz"


def test_state_reads_the_profile_selected_by_config_ex():
    coordinator = object.__new__(DahuaDataUpdateCoordinator)
    coordinator._channel = CHANNEL
    coordinator.data = {
        PROFILE_KEY: "0",
        PROFILE_CONFIG_EX_KEY: "Night",
        _exposure_key(CHANNEL, 0): "0",
        _exposure_key(CHANNEL, 1): "1",
    }

    assert coordinator.get_active_profile_index() == 1
    assert coordinator.get_exposure_anti_flicker() == "50_hz"


@pytest.mark.parametrize(
    ("profile_index", "anti_flicker", "expected_option"),
    [(0, "0", "outdoor"), (1, "1", "50_hz"), (2, "2", "60_hz")],
)
async def test_probe_uses_this_channels_current_profile(
    profile_index, anti_flicker, expected_option
):
    coordinator = object.__new__(DahuaDataUpdateCoordinator)
    coordinator._channel = CHANNEL
    coordinator._supports_exposure_anti_flicker = False
    coordinator._probe_refusals = {}
    coordinator.data = {
        PROFILE_KEY: str(profile_index),
        _exposure_key(CHANNEL, profile_index): anti_flicker,
    }
    coordinator.client = _ProbeClient(
        {PROFILE_KEY: str(profile_index)},
        {_exposure_key(CHANNEL, profile_index): anti_flicker},
    )

    assert await coordinator._async_probe_exposure_anti_flicker() is True
    assert coordinator.get_exposure_anti_flicker() == expected_option
    assert coordinator.client.config_names == ["VideoInExposure"]
    assert coordinator.supports_exposure_anti_flicker() is True
    assert (
        exposure_anti_flicker_option(
            coordinator.client.exposure, CHANNEL, profile_index
        )
        == expected_option
    )


class _ProbeClient:
    def __init__(self, mode, exposure):
        self.mode = mode
        self.exposure = exposure
        self.config_names = []

    async def async_get_video_in_mode(self):
        return self.mode

    async def async_get_config(self, name):
        self.config_names.append(name)
        return self.exposure

    async def async_get_exposure_anti_flicker_config(self):
        self.config_names.append("VideoInExposure")
        return self.exposure


class _WriteClient:
    def __init__(self, mode, exposure, write_error=None, mode_error=None):
        self.mode = mode
        self.exposure = exposure
        self.write_error = write_error
        self.mode_error = mode_error
        self.calls = []

    async def async_get_video_in_mode(self, fresh=False):
        self.calls.append(("VideoInMode", fresh))
        if self.mode_error is not None:
            raise self.mode_error
        return self.mode

    async def async_get_exposure_anti_flicker_config(self):
        self.calls.append(("getConfig", "VideoInExposure"))
        return self.exposure

    async def async_set_exposure_anti_flicker(self, channel, profile, value):
        self.calls.append(("setConfig", channel, profile, value))
        if self.write_error is not None:
            raise self.write_error


def _write_coordinator(
    mode, exposure, channel=CHANNEL, write_error=None, mode_error=None
):
    coordinator = object.__new__(DahuaDataUpdateCoordinator)
    coordinator._channel = channel
    coordinator.client = _WriteClient(mode, exposure, write_error, mode_error)
    coordinator.data = {PROFILE_KEY: "2"}
    return coordinator


@pytest.mark.parametrize(
    ("option", "device_value"),
    [("outdoor", 0), ("50_hz", 1), ("60_hz", 2)],
)
async def test_write_resolves_the_current_profile_and_channel(option, device_value):
    coordinator = _write_coordinator(
        {PROFILE_KEY: "1"},
        {_exposure_key(CHANNEL, 1): "0"},
    )

    await coordinator.async_set_exposure_anti_flicker(option)

    assert coordinator.client.calls == [
        ("VideoInMode", True),
        ("getConfig", "VideoInExposure"),
        ("setConfig", CHANNEL, 1, device_value),
    ]


async def test_write_keeps_general_profile_precedence_over_config_ex():
    coordinator = _write_coordinator(
        {PROFILE_KEY: "2", PROFILE_CONFIG_EX_KEY: "Night"},
        {_exposure_key(CHANNEL, 2): "0"},
    )

    await coordinator.async_set_exposure_anti_flicker("50_hz")

    assert coordinator.client.calls == [
        ("VideoInMode", True),
        ("getConfig", "VideoInExposure"),
        ("setConfig", CHANNEL, 2, 1),
    ]


async def test_write_does_not_use_a_cached_profile_when_the_current_read_fails():
    coordinator = _write_coordinator({}, {_exposure_key(CHANNEL, 2): "2"})

    with pytest.raises(ConnectionError):
        await coordinator.async_set_exposure_anti_flicker("50_hz")

    assert coordinator.client.calls == [("VideoInMode", True)]


async def test_failed_fresh_profile_read_does_not_use_coordinator_state():
    coordinator = _write_coordinator(
        {PROFILE_KEY: "0", PROFILE_CONFIG_EX_KEY: "Day"},
        {_exposure_key(CHANNEL, 0): "0"},
        mode_error=ConnectionError("fresh read failed"),
    )
    coordinator.data = {
        PROFILE_KEY: "0",
        PROFILE_CONFIG_EX_KEY: "Day",
        _exposure_key(CHANNEL, 0): "0",
    }

    with pytest.raises(ConnectionError, match="fresh read failed"):
        await coordinator.async_set_exposure_anti_flicker("50_hz")

    assert coordinator.client.calls == [("VideoInMode", True)]


async def test_warm_day_cache_is_bypassed_after_device_changes_to_night():
    device = "anti-flicker-cache-test:80"
    clear_host_cache(device)
    client = object.__new__(DahuaClient)
    client._device = device
    client._username = "user"
    client._session = object()
    client._refuse_if_closing = lambda: None
    physical_mode = {PROFILE_KEY: "0", PROFILE_CONFIG_EX_KEY: "Day"}
    mode_urls = []
    writes = []

    async def request(url, verify_ok=False):
        mode_urls.append(url)
        return physical_mode

    async def get_exposure():
        return {_exposure_key(CHANNEL, 1): "0"}

    async def set_exposure(channel, profile, value):
        writes.append((channel, profile, value))

    client._request = request
    client.async_get_exposure_anti_flicker_config = get_exposure
    client.async_set_exposure_anti_flicker = set_exposure

    try:
        assert await client.async_get_video_in_mode() == physical_mode
        physical_mode = {PROFILE_KEY: "0", PROFILE_CONFIG_EX_KEY: "Night"}

        coordinator = object.__new__(DahuaDataUpdateCoordinator)
        coordinator._channel = CHANNEL
        coordinator.client = client
        coordinator.data = {
            PROFILE_KEY: "0",
            PROFILE_CONFIG_EX_KEY: "Day",
            _exposure_key(CHANNEL, 0): "0",
        }

        await coordinator.async_set_exposure_anti_flicker("50_hz")
    finally:
        clear_host_cache(device)

    assert len(mode_urls) == 2
    assert mode_urls[0] == mode_urls[1]
    assert writes == [(CHANNEL, 1, 1)]


async def test_write_requires_the_exact_current_exposure_field():
    coordinator = _write_coordinator(
        {PROFILE_KEY: "1"},
        {_exposure_key(CHANNEL, 0): "0", _exposure_key(CHANNEL, 2): "2"},
    )

    with pytest.raises(ConnectionError):
        await coordinator.async_set_exposure_anti_flicker("50_hz")

    assert coordinator.client.calls == [
        ("VideoInMode", True),
        ("getConfig", "VideoInExposure"),
    ]


async def test_client_writes_only_the_selected_antiflicker_field():
    client = object.__new__(DahuaClient)
    calls = []

    async def get(url, verify_response=False):
        calls.append(url)
        return "OK"

    client.get = get

    await client.async_set_exposure_anti_flicker(CHANNEL, 2, 1)

    assert calls == [
        (
            "/cgi-bin/configManager.cgi?action=setConfig"
            "&VideoInExposure[3][2].AntiFlicker=1"
        )
    ]


async def test_client_reads_the_exact_exposure_table():
    client = object.__new__(DahuaClient)
    calls = []

    async def get(url):
        calls.append(url)
        return {}

    client.get = get

    await client.async_get_exposure_anti_flicker_config()

    assert calls == ["/cgi-bin/configManager.cgi?action=getConfig&name=VideoInExposure"]


@pytest.mark.parametrize(
    "arguments",
    [
        (-1, 0, 1),
        (CHANNEL, -1, 1),
        (CHANNEL, 2, True),
        (CHANNEL, 2, 3),
    ],
)
async def test_client_rejects_invalid_exposure_antiflicker_addresses(arguments):
    client = object.__new__(DahuaClient)
    client.get = pytest.fail

    with pytest.raises(ValueError):
        await client.async_set_exposure_anti_flicker(*arguments)


class _SelectCoordinator:
    def __init__(self, error=None):
        self.calls = []
        self.error = error
        self.refreshed = 0

    def get_serial_number(self):
        return "SERIAL1_3"

    def get_exposure_anti_flicker(self):
        return "50_hz"

    async def async_set_exposure_anti_flicker(self, option):
        self.calls.append(option)
        if self.error is not None:
            raise self.error

    async def async_refresh(self):
        self.refreshed += 1


def _entity(coordinator):
    entity = object.__new__(DahuaExposureAntiFlickerSelect)
    entity._coordinator = coordinator
    return entity


def test_select_exposes_translated_anti_flicker_options():
    assert EXPOSURE_ANTI_FLICKER_OPTIONS == ("outdoor", "50_hz", "60_hz")
    assert EXPOSURE_ANTI_FLICKER_VALUE_BY_OPTION == {
        "outdoor": 0,
        "50_hz": 1,
        "60_hz": 2,
    }
    assert EXPOSURE_ANTI_FLICKER_OPTION_BY_VALUE == {
        0: "outdoor",
        1: "50_hz",
        2: "60_hz",
    }
    assert _entity(_SelectCoordinator()).current_option == "50_hz"
    assert _entity(_SelectCoordinator()).options == list(EXPOSURE_ANTI_FLICKER_OPTIONS)


async def test_invalid_select_option_does_not_write_or_refresh():
    coordinator = _SelectCoordinator()

    await _entity(coordinator).async_select_option("invalid")

    assert coordinator.calls == []
    assert coordinator.refreshed == 0


async def test_failed_select_write_does_not_refresh():
    coordinator = _SelectCoordinator(RuntimeError("write refused"))

    with pytest.raises(RuntimeError, match="write refused"):
        await _entity(coordinator).async_select_option("outdoor")

    assert coordinator.calls == ["outdoor"]
    assert coordinator.refreshed == 0


async def test_successful_select_write_refreshes_once():
    coordinator = _SelectCoordinator()

    await _entity(coordinator).async_select_option("outdoor")

    assert coordinator.calls == ["outdoor"]
    assert coordinator.refreshed == 1


def test_option_state_translations_cover_every_option():
    path = Path(__file__).resolve().parents[2] / "custom_components" / "dahua"
    for language, labels in (
        ("en", {"outdoor": "Outdoor", "50_hz": "50 Hz", "60_hz": "60 Hz"}),
        ("es", {"outdoor": "Exterior", "50_hz": "50 Hz", "60_hz": "60 Hz"}),
    ):
        translation = json.loads(
            (path / "translations" / (language + ".json")).read_text(encoding="utf-8")
        )
        states = translation["entity"]["select"]["exposure_anti_flicker"]["state"]
        assert states == labels
