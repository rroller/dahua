"""Audio Controls use the measured keys and only a verified channel owner."""

from __future__ import annotations

import aiohttp
import pytest

from custom_components.dahua import coordinator as coordinator_module
from custom_components.dahua.audio import (
    AUDIO_CONTROLS,
    audio_config_key,
    audio_config_value,
    audio_row_owner,
    validate_audio_bool,
    validate_audio_percentage,
)
from custom_components.dahua.client import DahuaClient
from custom_components.dahua.coordinator import DahuaDataUpdateCoordinator
from custom_components.dahua.number import DahuaAudioVolumeNumber
from custom_components.dahua.switch import DahuaAudioNoiseFilterSwitch


@pytest.mark.parametrize(
    ("table", "channel", "recorder", "expected"),
    [
        ({"table.AudioInputVolume[0]": "47"}, 0, False, 0),
        ({"table.AudioInputVolume[3]": "55"}, 3, True, 3),
        ({"table.AudioInputVolume[0]": "47"}, 3, True, None),
        ({"table.AudioInputVolume[6]": "47"}, 3, True, None),
        ({"table.AudioInputVolume[0]": "47"}, 0, True, None),
    ],
)
def test_audio_ownership_requires_the_exact_channel_row(
    table, channel, recorder, expected
):
    assert (
        audio_row_owner(table, "microphone_volume", channel, recorder=recorder)
        == expected
    )


def test_scalar_volume_values_and_exact_noise_filter_states():
    assert audio_config_key("microphone_volume", 0) == "table.AudioInputVolume[0]"
    assert audio_config_key("speaker_volume", 3) == "table.AudioOutputVolume[3]"
    assert (
        audio_config_value({"table.AudioInputVolume[0]": "47"}, "microphone_volume", 0)
        == 47
    )
    assert (
        audio_config_value({"table.AudioOutputVolume[3]": "73"}, "speaker_volume", 3)
        == 73
    )
    assert (
        audio_config_value(
            {"table.AudioInDenoise[3].enable": "true"}, "noise_filter", 3
        )
        is True
    )
    assert (
        audio_config_value(
            {"table.AudioInDenoise[3].enable": "false"}, "noise_filter", 3
        )
        is False
    )
    assert audio_config_value({}, "noise_filter", 3) is None


@pytest.mark.parametrize("value", [0, 100, 47.0])
def test_volume_validation_accepts_only_exact_integer_steps(value):
    assert validate_audio_percentage(value) == int(value)


@pytest.mark.parametrize("value", [-1, 101, True, False, 47.9, "47", None])
def test_volume_validation_rejects_invalid_values(value):
    with pytest.raises(ValueError):
        validate_audio_percentage(value)


@pytest.mark.parametrize("value", [1, 0, "true", None])
def test_noise_filter_validation_rejects_non_booleans(value):
    with pytest.raises(ValueError):
        validate_audio_bool(value)


class _Client:
    def __init__(self):
        self.requests = []
        self.refreshes = 0
        self.failure = None

    async def async_set_audio_config(self, name, owner, value):
        self.requests.append((name, owner, value))
        if self.failure:
            raise self.failure


class _Coordinator:
    def __init__(self, client=None):
        self.client = client or _Client()
        self.data = {}
        self._owners = {
            "microphone_volume": 0,
            "speaker_volume": 3,
            "noise_filter": 3,
        }

    def audio_control_owner(self, key):
        return self._owners.get(key)

    async def async_refresh(self):
        self.client.refreshes += 1


@pytest.mark.parametrize(
    ("name", "owner", "value", "url"),
    [
        (
            "microphone_volume",
            0,
            42,
            "/cgi-bin/configManager.cgi?action=setConfig&table.AudioInputVolume[0]=42",
        ),
        (
            "speaker_volume",
            3,
            73,
            "/cgi-bin/configManager.cgi?action=setConfig&table.AudioOutputVolume[3]=73",
        ),
        (
            "noise_filter",
            3,
            True,
            "/cgi-bin/configManager.cgi?action=setConfig&table.AudioInDenoise[3].enable=true",
        ),
        (
            "noise_filter",
            3,
            False,
            "/cgi-bin/configManager.cgi?action=setConfig&table.AudioInDenoise[3].enable=false",
        ),
    ],
)
async def test_client_writes_exact_targeted_audio_urls(
    monkeypatch, name, owner, value, url
):
    client = DahuaClient.__new__(DahuaClient)
    client._device = "device:80"
    sent = []
    invalidated = []

    async def request(request_url, reject_declined=False):
        sent.append((request_url, reject_declined))

    monkeypatch.setattr(
        "custom_components.dahua.client.clear_host_cache", invalidated.append
    )
    client._request = request

    await client.async_set_audio_config(name, owner, value)

    assert sent == [(url, True)]
    assert invalidated == ["device:80"]


@pytest.mark.parametrize("value", [-1, 101, True, False, 47.9, "47", None])
async def test_invalid_volume_is_rejected_before_any_request_or_cache_change(
    monkeypatch, value
):
    client = DahuaClient.__new__(DahuaClient)
    client._device = "device:80"
    requested = []
    invalidated = []

    async def request(*args, **kwargs):
        requested.append(args)

    monkeypatch.setattr(
        "custom_components.dahua.client.clear_host_cache", invalidated.append
    )
    client._request = request

    with pytest.raises(ValueError):
        await client.async_set_audio_config("microphone_volume", 0, value)

    assert requested == []
    assert invalidated == []


async def test_failed_write_does_not_invalidate_cache(monkeypatch):
    client = DahuaClient.__new__(DahuaClient)
    client._device = "device:80"
    invalidated = []

    async def request(*args, **kwargs):
        raise RuntimeError("write refused")

    monkeypatch.setattr(
        "custom_components.dahua.client.clear_host_cache", invalidated.append
    )
    client._request = request

    with pytest.raises(RuntimeError, match="write refused"):
        await client.async_set_audio_config("noise_filter", 0, True)

    assert invalidated == []


async def test_audio_table_read_uses_the_raw_get_path():
    client = DahuaClient.__new__(DahuaClient)
    requested = []

    async def get(url):
        requested.append(url)
        return {}

    client.get = get
    assert await client.async_get_audio_config("noise_filter") == {}
    assert requested == [
        "/cgi-bin/configManager.cgi?action=getConfig&name=AudioInDenoise"
    ]


async def test_number_write_refreshes_once_and_does_not_truncate():
    coordinator = _Coordinator()
    entity = object.__new__(DahuaAudioVolumeNumber)
    entity._coordinator = coordinator
    entity._key = "speaker_volume"
    entity._owner = 3

    await entity.async_set_native_value(47.0)

    assert coordinator.client.requests == [("speaker_volume", 3, 47.0)]
    assert coordinator.client.refreshes == 1


async def test_rejected_number_write_does_not_refresh():
    coordinator = _Coordinator()
    coordinator.client.failure = ValueError("invalid")
    entity = object.__new__(DahuaAudioVolumeNumber)
    entity._coordinator = coordinator
    entity._key = "microphone_volume"
    entity._owner = 0

    with pytest.raises(ValueError, match="invalid"):
        await entity.async_set_native_value(47.9)

    assert coordinator.client.refreshes == 0


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("true", True), ("false", False), (None, None)],
)
def test_noise_filter_state_preserves_true_false_and_unknown(raw, expected):
    coordinator = _Coordinator()
    coordinator.data = (
        {"table.AudioInDenoise[3].enable": raw} if raw is not None else {}
    )
    entity = object.__new__(DahuaAudioNoiseFilterSwitch)
    entity._coordinator = coordinator
    entity._owner = 3

    assert entity.is_on is expected


async def test_noise_filter_write_refreshes_once_and_targets_only_enable():
    coordinator = _Coordinator()
    entity = object.__new__(DahuaAudioNoiseFilterSwitch)
    entity._coordinator = coordinator
    entity._owner = 3

    await entity.async_turn_on()

    assert coordinator.client.requests == [("noise_filter", 3, True)]
    assert coordinator.client.refreshes == 1


async def test_noise_filter_failed_write_does_not_refresh():
    coordinator = _Coordinator()
    coordinator.client.failure = RuntimeError("refused")
    entity = object.__new__(DahuaAudioNoiseFilterSwitch)
    entity._coordinator = coordinator
    entity._owner = 3

    with pytest.raises(RuntimeError, match="refused"):
        await entity.async_turn_off()

    assert coordinator.client.refreshes == 0


def _bare_coordinator(channel=3, recorder=False):
    coordinator = object.__new__(DahuaDataUpdateCoordinator)
    coordinator._channel = channel
    coordinator._device_class = "NVR" if recorder else "IPC"
    coordinator._audio_control_owners = {}
    coordinator._probe_refusals = {}
    coordinator._address = "device"
    coordinator.data = {}
    return coordinator


async def test_audio_capabilities_are_independent_and_late_probe_discovers_owner():
    coordinator = _bare_coordinator()

    class Client:
        async def async_get_audio_config(self, name):
            if name == "microphone_volume":
                return {"table.AudioInputVolume[3]": "47"}
            return {}

    coordinator.client = Client()

    assert await coordinator._async_retry_audio_input_volume() is True
    assert coordinator.supports_audio_input_volume() is True
    assert coordinator.supports_audio_output_volume() is False
    assert coordinator.supports_audio_in_denoise() is False
    assert coordinator.audio_control_owner("microphone_volume") == 3


async def test_successful_empty_probe_is_not_recorded_as_a_refusal():
    coordinator = _bare_coordinator()

    class Client:
        async def async_get_audio_config(self, _name):
            return {}

    coordinator.client = Client()

    assert await coordinator._async_probe_audio_control("speaker_volume") is False
    assert coordinator._probe_refusals == {}
    assert coordinator.audio_control_owner("speaker_volume") is None


async def test_http_refusal_is_not_converted_to_an_empty_success():
    coordinator = _bare_coordinator()

    class Client:
        async def async_get_audio_config(self, _name):
            raise aiohttp.ClientResponseError(None, (), status=403)

    coordinator.client = Client()

    assert await coordinator._async_probe_audio_control("noise_filter") is False
    assert coordinator._probe_refusals["audio_in_denoise"]["status"] == 403


async def test_poll_refusal_carries_only_that_audio_tables_previous_keys():
    coordinator = _bare_coordinator()
    coordinator.data = {
        "table.AudioInputVolume[3]": "47",
        "table.AudioOutputVolume[3]": "73",
        "table.AudioInDenoise[3].enable": "true",
    }

    class Client:
        async def async_get_audio_config(self, _name):
            raise aiohttp.ClientResponseError(None, (), status=403)

    coordinator.client = Client()

    assert await coordinator._async_fetch_audio_control("microphone_volume") == {
        "table.AudioInputVolume[3]": "47"
    }
    assert await coordinator._async_fetch_audio_control("noise_filter") == {
        "table.AudioInDenoise[3].enable": "true"
    }


async def test_successful_empty_poll_stays_distinct_from_a_refusal():
    coordinator = _bare_coordinator()

    class Client:
        async def async_get_audio_config(self, _name):
            return {}

    coordinator.client = Client()
    assert await coordinator._async_fetch_audio_control("microphone_volume") == {}


def test_audio_controls_have_separate_retryable_capabilities():
    assert {
        AUDIO_CONTROLS[name][3] for name in AUDIO_CONTROLS
    } <= coordinator_module.RETRYABLE_CAPABILITIES.keys()
