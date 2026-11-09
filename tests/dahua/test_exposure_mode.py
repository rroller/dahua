"""Profile-aware Exposure Mode and its Anti-Flicker dependency."""

from types import SimpleNamespace

import pytest
from aiohttp import ClientResponseError

from custom_components.dahua.client import DahuaClient
from custom_components.dahua.const import (
    EXPOSURE_MODE_OPTION_BY_VALUE,
    EXPOSURE_MODE_VALUE_BY_OPTION,
    LEGACY_EXPOSURE_MODE_OPTION_BY_VALUE,
    LEGACY_EXPOSURE_MODE_VALUE_BY_OPTION,
)
from custom_components.dahua.coordinator import (
    RETRYABLE_CAPABILITIES,
    DahuaDataUpdateCoordinator,
    ExposureModeTransport,
    exposure_mode_option,
    exposure_mode_options,
    exposure_mode_value,
    legacy_exposure_mode_option,
)
from custom_components.dahua.select import (
    DahuaExposureAntiFlickerSelect,
    DahuaExposureModeSelect,
)

CHANNEL = 3
PROFILE = 1
PROFILE_KEY = f"table.VideoInMode[{CHANNEL}].Config[0]"
CONFIG_EX_KEY = f"table.VideoInMode[{CHANNEL}].ConfigEx"


def _mode_key(channel=CHANNEL, profile=PROFILE):
    return f"table.VideoInExposure[{channel}][{profile}].Mode"


def _anti_flicker_key(channel=CHANNEL, profile=PROFILE):
    return f"table.VideoInExposure[{channel}][{profile}].AntiFlicker"


def _coordinator(
    mode_data,
    exposure_data,
    anti_flicker_supported=True,
    legacy_options=None,
):
    coordinator = object.__new__(DahuaDataUpdateCoordinator)
    coordinator._channel = CHANNEL
    coordinator._supports_exposure_mode = True
    coordinator._supports_exposure_anti_flicker = anti_flicker_supported
    coordinator._image_option_fields = (
        frozenset({"ExposureMode"}) if legacy_options is not None else frozenset()
    )
    coordinator.is_indoor_monitor_without_video = lambda: False
    coordinator.data = {**mode_data, **exposure_data}
    if legacy_options is not None:
        coordinator.data.update(legacy_options)
    coordinator.client = _WriteClient(mode_data, exposure_data)
    if legacy_options is not None:
        coordinator.client.video_in_options = legacy_options
    coordinator.refreshes = 0

    async def refresh():
        coordinator.refreshes += 1
        coordinator.data.update(coordinator.client.exposure)

    coordinator.async_refresh = refresh
    return coordinator


class _WriteClient:
    def __init__(self, mode_data, exposure):
        self.mode_data = mode_data
        self.exposure = exposure
        self.calls = []
        self.mode_error = None
        self.exposure_error = None
        self.video_in_options = {}
        self.video_in_options_error = None

    async def async_get_video_in_mode(self, fresh=False):
        self.calls.append(("VideoInMode", fresh))
        if self.mode_error:
            raise self.mode_error
        return self.mode_data

    async def async_get_exposure_anti_flicker_config(self, fresh=False):
        self.calls.append(("VideoInExposure", fresh))
        if self.exposure_error:
            raise self.exposure_error
        return self.exposure

    async def async_set_exposure_mode(self, channel, profile, value):
        self.calls.append(("set_Mode", channel, profile, value))

    async def async_get_video_in_options(self, fresh=False):
        self.calls.append(("VideoInOptions", fresh))
        if self.video_in_options_error:
            raise self.video_in_options_error
        return self.video_in_options

    async def async_set_legacy_exposure_mode(self, channel, value):
        self.calls.append(("set_legacy_Mode", channel, value))

    async def async_set_exposure_anti_flicker(self, channel, profile, value):
        self.calls.append(("set_AntiFlicker", channel, profile, value))
        self.exposure[_anti_flicker_key(channel, profile)] = str(value)


@pytest.mark.parametrize(
    ("option", "value"),
    [
        ("Auto", 0),
        ("Gain Priority", 7),
        ("Shutter Priority", 8),
        ("Manual", 4),
    ],
)
def test_mode_option_mappings_are_bidirectional(option, value):
    assert EXPOSURE_MODE_VALUE_BY_OPTION[option] == value
    assert EXPOSURE_MODE_OPTION_BY_VALUE[value] == option
    assert exposure_mode_option({_mode_key(): str(value)}, CHANNEL, PROFILE) == option
    assert exposure_mode_value({_mode_key(): value}, CHANNEL, PROFILE) == value


@pytest.mark.parametrize("value", [True, False, 1, 2, 9, 7.0, "bad", None])
def test_unknown_or_malformed_mode_values_are_not_guessed(value):
    assert exposure_mode_option({_mode_key(): value}, CHANNEL, PROFILE) is None


def test_mode_state_does_not_fall_back_to_another_channel_or_profile():
    data = {
        _mode_key(channel=0, profile=PROFILE): "0",
        _mode_key(channel=CHANNEL, profile=0): "7",
    }

    assert exposure_mode_option(data, CHANNEL, PROFILE) is None


@pytest.mark.parametrize(
    ("value", "option"),
    [(0, "Auto"), (1, "Gain Priority"), (2, "Shutter Priority")],
)
def test_legacy_mode_mapping_is_distinct_from_profile_mode(value, option):
    data = {f"table.VideoInOptions[{CHANNEL}].ExposureMode": str(value)}
    assert legacy_exposure_mode_option(data, CHANNEL) == option
    assert LEGACY_EXPOSURE_MODE_OPTION_BY_VALUE[value] == option
    assert LEGACY_EXPOSURE_MODE_VALUE_BY_OPTION[option] == value
    assert option != "Manual"


@pytest.mark.parametrize("value", [True, False, "3", "bad", None])
def test_invalid_legacy_mode_values_are_unknown(value):
    assert (
        legacy_exposure_mode_option(
            {f"table.VideoInOptions[{CHANNEL}].ExposureMode": value}, CHANNEL
        )
        is None
    )


def test_profile_transport_precedes_legacy_for_state_and_options():
    coordinator = _coordinator(
        {PROFILE_KEY: str(PROFILE)},
        {_mode_key(): "4"},
        anti_flicker_supported=False,
        legacy_options={f"table.VideoInOptions[{CHANNEL}].ExposureMode": "1"},
    )

    assert coordinator.get_exposure_mode_transport() is ExposureModeTransport.PROFILE
    assert coordinator.get_exposure_mode_option() == "Manual"
    assert coordinator.get_exposure_mode_options() == (
        "Auto",
        "Gain Priority",
        "Shutter Priority",
        "Manual",
    )


def test_legacy_transport_works_without_profile_context_or_video_in_exposure():
    coordinator = _coordinator(
        {},
        {},
        legacy_options={f"table.VideoInOptions[{CHANNEL}].ExposureMode": "2"},
    )
    coordinator._supports_exposure_mode = False

    assert coordinator.get_exposure_mode_transport() is ExposureModeTransport.LEGACY
    assert coordinator.get_exposure_mode_option() == "Shutter Priority"
    assert coordinator.get_exposure_mode_options() == (
        "Auto",
        "Gain Priority",
        "Shutter Priority",
    )
    assert coordinator.supports_exposure_mode() is True


async def test_profile_transport_uses_profile_writer_when_legacy_also_exists():
    coordinator = _coordinator(
        {PROFILE_KEY: str(PROFILE)},
        {_mode_key(): "0"},
        anti_flicker_supported=False,
        legacy_options={f"table.VideoInOptions[{CHANNEL}].ExposureMode": "1"},
    )

    await coordinator.async_set_exposure_mode("Gain Priority")

    assert coordinator.client.calls == [
        ("VideoInMode", True),
        ("VideoInExposure", True),
        ("set_Mode", CHANNEL, PROFILE, 7),
    ]


async def test_profile_write_failure_does_not_fall_back_to_legacy():
    coordinator = _coordinator(
        {PROFILE_KEY: str(PROFILE)},
        {_mode_key(): "0"},
        legacy_options={f"table.VideoInOptions[{CHANNEL}].ExposureMode": "1"},
    )
    coordinator.client.exposure_error = ConnectionError("profile read failed")

    with pytest.raises(ConnectionError, match="profile read failed"):
        await coordinator.async_set_exposure_mode("Gain Priority")

    assert coordinator.client.calls == [
        ("VideoInMode", True),
        ("VideoInExposure", True),
    ]


async def test_legacy_transport_uses_only_fresh_legacy_context_and_writer():
    coordinator = _coordinator(
        {},
        {},
        legacy_options={f"table.VideoInOptions[{CHANNEL}].ExposureMode": "2"},
    )
    coordinator._supports_exposure_mode = False

    await coordinator.async_set_exposure_mode("Gain Priority")

    assert coordinator.client.calls == [
        ("VideoInOptions", True),
        ("set_legacy_Mode", CHANNEL, 1),
    ]


async def test_legacy_write_rejects_manual_and_missing_fresh_field():
    coordinator = _coordinator({}, {}, legacy_options={})
    coordinator._supports_exposure_mode = False

    with pytest.raises(ValueError, match="Unsupported legacy"):
        await coordinator.async_set_exposure_mode("Manual")
    assert coordinator.client.calls == []

    with pytest.raises(ConnectionError, match="VideoInOptions"):
        await coordinator.async_set_exposure_mode("Auto")
    assert coordinator.client.calls == [("VideoInOptions", True)]


async def test_failed_legacy_write_context_does_not_call_writer():
    coordinator = _coordinator({}, {}, legacy_options={})
    coordinator._supports_exposure_mode = False
    coordinator.client.video_in_options_error = ConnectionError("read failed")

    with pytest.raises(ConnectionError, match="read failed"):
        await coordinator.async_set_exposure_mode("Auto")
    assert coordinator.client.calls == [("VideoInOptions", True)]


@pytest.mark.parametrize(
    ("anti_flicker", "expected"),
    [
        ("0", ("Auto", "Gain Priority", "Shutter Priority", "Manual")),
        ("1", ("Auto", "Manual")),
        ("2", ("Auto", "Manual")),
    ],
)
def test_mode_options_follow_active_profile_antiflicker(anti_flicker, expected):
    data = {
        _mode_key(): "0",
        _anti_flicker_key(): anti_flicker,
        _anti_flicker_key(channel=0): "1",
    }

    assert exposure_mode_options(data, CHANNEL, PROFILE, True) == expected


def test_mode_without_antiflicker_has_all_options():
    assert exposure_mode_options({_mode_key(): "0"}, CHANNEL, PROFILE) == (
        "Auto",
        "Gain Priority",
        "Shutter Priority",
        "Manual",
    )


def test_mode_without_antiflicker_ignores_a_residual_field():
    assert exposure_mode_options(
        {_mode_key(): "0", _anti_flicker_key(): "1"},
        CHANNEL,
        PROFILE,
        anti_flicker_supported=False,
    ) == ("Auto", "Gain Priority", "Shutter Priority", "Manual")


def test_missing_or_malformed_supported_antiflicker_fails_closed():
    assert exposure_mode_options({_mode_key(): "0"}, CHANNEL, PROFILE, True) == ()
    assert (
        exposure_mode_options(
            {_mode_key(): "0", _anti_flicker_key(): "9"},
            CHANNEL,
            PROFILE,
            anti_flicker_supported=True,
        )
        == ()
    )


def test_mode_probe_is_independent_and_retryable():
    assert (
        RETRYABLE_CAPABILITIES["exposure_mode"]
        is DahuaDataUpdateCoordinator._async_retry_exposure_mode
    )


async def test_mode_retry_discovers_a_new_profile_mode_capability():
    coordinator = _coordinator(
        {PROFILE_KEY: str(PROFILE)},
        {_mode_key(profile=PROFILE): "7"},
        anti_flicker_supported=False,
    )
    coordinator._supports_exposure_mode = False

    assert coordinator.supports_exposure_mode() is False
    assert coordinator.client.mode_data[PROFILE_KEY] == str(PROFILE)
    assert coordinator.client.exposure == {_mode_key(profile=PROFILE): "7"}
    assert await coordinator._async_retry_exposure_mode() is True
    assert coordinator.supports_exposure_mode() is True


async def test_mode_probe_uses_the_same_channel_and_resolved_profile():
    coordinator = _coordinator(
        {PROFILE_KEY: "0", CONFIG_EX_KEY: "Night"},
        {_mode_key(): "8"},
        anti_flicker_supported=False,
    )
    coordinator._supports_exposure_mode = False

    assert await coordinator._async_probe_exposure_mode() is True
    assert coordinator.supports_exposure_mode() is True
    assert coordinator.client.calls == [
        ("VideoInMode", False),
        ("VideoInExposure", False),
    ]


async def test_profile_probe_refusal_preserves_established_legacy_support():
    coordinator = _coordinator(
        {},
        {},
        legacy_options={f"table.VideoInOptions[{CHANNEL}].ExposureMode": "0"},
    )
    coordinator._supports_exposure_mode = False
    coordinator.client.mode_error = TimeoutError("VideoInMode unavailable")

    assert await coordinator._async_probe_exposure_mode() is False
    assert coordinator.get_exposure_mode_transport() is ExposureModeTransport.LEGACY
    assert coordinator.get_exposure_mode_option() == "Auto"


async def test_mode_write_uses_fresh_profile_and_exact_mode_owner():
    mode_data = {
        PROFILE_KEY: "2",
        CONFIG_EX_KEY: "Night",
        "table.VideoInMode[0].Config[0]": "0",
    }
    exposure = {
        _mode_key(channel=CHANNEL, profile=2): "0",
        _anti_flicker_key(channel=CHANNEL, profile=2): "0",
        _mode_key(channel=CHANNEL, profile=1): "4",
    }
    coordinator = _coordinator(mode_data, exposure)

    mode_entity = object.__new__(DahuaExposureModeSelect)
    mode_entity._coordinator = coordinator
    await mode_entity.async_select_option("Shutter Priority")

    assert coordinator.client.calls == [
        ("VideoInMode", True),
        ("VideoInExposure", True),
        ("set_Mode", CHANNEL, 2, 8),
    ]
    assert coordinator.data[_mode_key(channel=CHANNEL, profile=2)] == "0"
    assert coordinator.refreshes == 1


@pytest.mark.parametrize(
    ("anti_flicker", "option"),
    [
        ("1", "Gain Priority"),
        ("1", "Shutter Priority"),
        ("2", "Gain Priority"),
        ("2", "Shutter Priority"),
    ],
)
async def test_invalid_mode_for_current_antiflicker_is_rejected_without_write(
    anti_flicker, option
):
    coordinator = _coordinator(
        {PROFILE_KEY: "1"},
        {_mode_key(): "0", _anti_flicker_key(): anti_flicker},
    )

    with pytest.raises(ValueError, match="not valid"):
        await coordinator.async_set_exposure_mode(option)

    assert coordinator.client.calls == [
        ("VideoInMode", True),
        ("VideoInExposure", True),
    ]
    assert coordinator.refreshes == 0


@pytest.mark.parametrize(
    "option", ["Auto", "Gain Priority", "Shutter Priority", "Manual"]
)
async def test_mode_without_antiflicker_allows_all_supported_options(option):
    coordinator = _coordinator(
        {PROFILE_KEY: "1"},
        {_mode_key(): "0", _anti_flicker_key(): "1"},
        anti_flicker_supported=False,
    )

    await coordinator.async_set_exposure_mode(option)

    assert coordinator.client.calls[-1] == (
        "set_Mode",
        CHANNEL,
        1,
        EXPOSURE_MODE_VALUE_BY_OPTION[option],
    )


@pytest.mark.parametrize(
    ("anti_flicker", "option", "value"),
    [
        ("0", "Gain Priority", 7),
        ("0", "Manual", 4),
        ("1", "Auto", 0),
        ("1", "Manual", 4),
        ("2", "Auto", 0),
        ("2", "Manual", 4),
    ],
)
async def test_valid_mode_for_current_antiflicker_is_written(
    anti_flicker, option, value
):
    coordinator = _coordinator(
        {PROFILE_KEY: "1"},
        {_mode_key(): "0", _anti_flicker_key(): anti_flicker},
    )

    await coordinator.async_set_exposure_mode(option)

    assert coordinator.client.calls[-1] == ("set_Mode", CHANNEL, 1, value)


async def test_failed_fresh_exposure_read_cannot_use_stale_mode_or_write():
    coordinator = _coordinator(
        {PROFILE_KEY: "1"},
        {_mode_key(): "0", _anti_flicker_key(): "0"},
    )
    coordinator.client.exposure_error = ConnectionError("fresh read failed")

    mode_entity = object.__new__(DahuaExposureModeSelect)
    mode_entity._coordinator = coordinator
    with pytest.raises(ConnectionError, match="fresh read failed"):
        await mode_entity.async_select_option("Gain Priority")

    assert coordinator.client.calls == [
        ("VideoInMode", True),
        ("VideoInExposure", True),
    ]
    assert coordinator.refreshes == 0


async def test_failed_fresh_profile_read_does_not_write_or_refresh():
    coordinator = _coordinator(
        {PROFILE_KEY: "1"},
        {_mode_key(): "0", _anti_flicker_key(): "0"},
    )
    coordinator.client.mode_error = ConnectionError("fresh profile read failed")
    mode_entity = object.__new__(DahuaExposureModeSelect)
    mode_entity._coordinator = coordinator

    with pytest.raises(ConnectionError, match="fresh profile read failed"):
        await mode_entity.async_select_option("Auto")

    assert coordinator.client.calls == [("VideoInMode", True)]
    assert coordinator.refreshes == 0


async def test_invalid_ha_option_causes_no_read_write_or_refresh():
    coordinator = _coordinator(
        {PROFILE_KEY: "1"},
        {_mode_key(): "0", _anti_flicker_key(): "0"},
    )
    mode_entity = object.__new__(DahuaExposureModeSelect)
    mode_entity._coordinator = coordinator

    await mode_entity.async_select_option("Not a Mode")

    assert coordinator.client.calls == []
    assert coordinator.refreshes == 0


async def test_stale_antiflicker_cannot_authorize_mode_write():
    coordinator = _coordinator(
        {PROFILE_KEY: "1"},
        {_mode_key(): "0", _anti_flicker_key(): "0"},
    )
    coordinator.client.exposure = {
        _mode_key(): "0",
        _anti_flicker_key(): "1",
    }
    mode_entity = object.__new__(DahuaExposureModeSelect)
    mode_entity._coordinator = coordinator

    with pytest.raises(ValueError, match="not valid"):
        await mode_entity.async_select_option("Gain Priority")

    assert coordinator.client.calls == [
        ("VideoInMode", True),
        ("VideoInExposure", True),
    ]
    assert coordinator.refreshes == 0


async def test_antiflicker_refresh_updates_same_mode_entity_options():
    coordinator = _coordinator(
        {PROFILE_KEY: "1"},
        {_mode_key(): "8", _anti_flicker_key(): "0"},
    )
    mode_entity = object.__new__(DahuaExposureModeSelect)
    mode_entity._coordinator = coordinator
    anti_entity = object.__new__(DahuaExposureAntiFlickerSelect)
    anti_entity._coordinator = coordinator

    assert mode_entity.options == [
        "Auto",
        "Gain Priority",
        "Shutter Priority",
        "Manual",
    ]
    await anti_entity.async_select_option("50_hz")

    assert mode_entity.options == ["Auto", "Manual"]
    assert mode_entity.current_option == "Shutter Priority"
    assert coordinator.client.calls[-1] == ("set_AntiFlicker", CHANNEL, 1, 1)
    assert coordinator.refreshes == 1


def _client():
    client = object.__new__(DahuaClient)
    client._device = "10.0.0.5:80"
    client._username = "user"
    return client


@pytest.mark.parametrize("value", [0, 7, 8, 4])
async def test_client_writes_only_the_exact_profile_mode_field(value):
    client = _client()
    calls = []

    async def get(url):
        calls.append(url)
        return "OK"

    client.get = get
    await client.async_set_exposure_mode(CHANNEL, PROFILE, value)

    assert calls == [
        f"/cgi-bin/configManager.cgi?action=setConfig&VideoInExposure[{CHANNEL}][{PROFILE}].Mode={value}"
    ]


async def test_client_mode_write_uses_exact_rpc2_field_when_cgi_is_absent():
    client = _client()
    fallback = []

    async def get(_url):
        raise ClientResponseError(None, (), status=404)

    async def set_field(field, value):
        fallback.append((field, value))

    client.get = get
    client._rpc2_set_config_field = set_field
    await client.async_set_exposure_mode(CHANNEL, PROFILE, 7)

    assert fallback == [(f"VideoInExposure[{CHANNEL}][{PROFILE}].Mode", 7)]


async def test_client_propagates_non_absent_mode_write_errors():
    client = _client()
    error = ClientResponseError(
        request_info=SimpleNamespace(
            real_url="http://device.invalid/configManager.cgi"
        ),
        history=(),
        status=403,
    )

    async def get(_url):
        raise error

    async def fallback(*_args):
        pytest.fail("RPC2 fallback must only follow a missing CGI endpoint")

    client.get = get
    client._rpc2_set_config_field = fallback

    with pytest.raises(ClientResponseError) as caught:
        await client.async_set_exposure_mode(CHANNEL, PROFILE, 7)
    assert caught.value is error


@pytest.mark.parametrize(
    "arguments",
    [
        (-1, PROFILE, 0),
        (True, PROFILE, 0),
        (CHANNEL, -1, 0),
        (CHANNEL, True, 0),
        (CHANNEL, PROFILE, True),
        (CHANNEL, PROFILE, 1),
        (CHANNEL, PROFILE, 2),
        (CHANNEL, PROFILE, 9),
    ],
)
async def test_client_rejects_invalid_mode_write_values(arguments):
    client = _client()
    client.get = pytest.fail

    with pytest.raises(ValueError):
        await client.async_set_exposure_mode(*arguments)
