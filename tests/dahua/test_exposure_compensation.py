"""Profile-aware Exposure Compensation reads and writes."""

import pytest
from aiohttp import ClientResponseError

from custom_components.dahua import client as client_module
from custom_components.dahua.client import DahuaClient
from custom_components.dahua.coordinator import (
    RETRYABLE_CAPABILITIES,
    DahuaDataUpdateCoordinator,
    exposure_compensation_value,
)
from custom_components.dahua.number import DahuaExposureCompensationNumber

CHANNEL = 3
PROFILE_KEY = f"table.VideoInMode[{CHANNEL}].Config[0]"
PROFILE_CONFIG_EX_KEY = f"table.VideoInMode[{CHANNEL}].ConfigEx"
COMPENSATION_KEY = f"table.VideoInExposure[{CHANNEL}][1].Compensation"
MODE_KEY = f"table.VideoInExposure[{CHANNEL}][1].Mode"


def test_compensation_probe_is_registered_for_late_discovery():
    assert (
        RETRYABLE_CAPABILITIES["exposure_compensation"]
        is DahuaDataUpdateCoordinator._async_retry_exposure_compensation
    )


def _profile_key(channel, profile):
    return f"table.VideoInExposure[{channel}][{profile}].Compensation"


@pytest.mark.parametrize(
    ("value", "expected"),
    [(0, 0), (15, 15), (50, 50), (62, 62), (100, 100), ("26", 26)],
)
def test_exposure_compensation_value_accepts_supported_values(value, expected):
    assert (
        exposure_compensation_value({COMPENSATION_KEY: value}, CHANNEL, 1) == expected
    )


@pytest.mark.parametrize(
    "value",
    [True, False, -1, 101, "-1", "101", "nope", 1.5],
)
def test_exposure_compensation_value_rejects_invalid_values(value):
    assert exposure_compensation_value({COMPENSATION_KEY: value}, CHANNEL, 1) is None


def _coordinator(channel, data, supported=True, mode_supported=False):
    coordinator = object.__new__(DahuaDataUpdateCoordinator)
    coordinator._channel = channel
    coordinator._supports_exposure_compensation = supported
    coordinator._supports_exposure_mode = mode_supported
    coordinator.data = data
    return coordinator


def test_unknown_mode_is_not_assumed_to_be_manual():
    coordinator = _coordinator(
        CHANNEL,
        {
            PROFILE_KEY: "1",
            PROFILE_CONFIG_EX_KEY: "Night",
            MODE_KEY: "unknown",
            COMPENSATION_KEY: "50",
        },
    )
    assert coordinator.exposure_compensation_available() is True
    assert coordinator.get_exposure_compensation() == 50


def test_manual_mode_disables_compensation_without_clearing_its_stored_value():
    data = {
        PROFILE_KEY: "1",
        MODE_KEY: "4",
        COMPENSATION_KEY: "50",
    }
    coordinator = _coordinator(
        CHANNEL,
        data,
        mode_supported=True,
    )
    assert coordinator.exposure_compensation_available() is False
    assert coordinator.get_exposure_compensation() is None
    entity = object.__new__(DahuaExposureCompensationNumber)
    entity._coordinator = coordinator
    assert entity.native_value is None
    assert data[COMPENSATION_KEY] == "50"


@pytest.mark.parametrize("mode", ["0", "7", "8"])
def test_compensation_is_available_under_supported_nonmanual_modes(mode):
    coordinator = _coordinator(
        CHANNEL,
        {
            PROFILE_KEY: "1",
            MODE_KEY: mode,
            COMPENSATION_KEY: "50",
        },
        mode_supported=True,
    )

    assert coordinator.exposure_compensation_available() is True
    assert coordinator.get_exposure_compensation() == 50


@pytest.mark.parametrize("mode_supported", [False, True])
def test_mode_absent_keeps_independently_valid_compensation_available(
    mode_supported,
):
    coordinator = _coordinator(
        CHANNEL,
        {PROFILE_KEY: "1", COMPENSATION_KEY: "50"},
        mode_supported=mode_supported,
    )
    assert coordinator.exposure_compensation_available() is True


def test_compensation_requires_capability_and_a_current_profile_value():
    data = {PROFILE_KEY: "1", COMPENSATION_KEY: "50"}
    assert (
        _coordinator(CHANNEL, data, supported=False).exposure_compensation_available()
        is False
    )
    assert (
        _coordinator(
            CHANNEL, {PROFILE_KEY: "1"}, supported=True
        ).exposure_compensation_available()
        is False
    )


def test_compensation_state_never_falls_back_to_channel_zero():
    coordinator = _coordinator(
        CHANNEL,
        {
            "table.VideoInMode[0].Config[0]": "1",
            "table.VideoInExposure[0][1].Compensation": "45",
        },
    )
    assert coordinator.get_exposure_compensation() is None
    assert coordinator.exposure_compensation_available() is False


def test_compensation_state_never_falls_back_to_another_profile():
    coordinator = _coordinator(
        CHANNEL,
        {
            PROFILE_KEY: "1",
            _profile_key(CHANNEL, 0): "40",
            _profile_key(CHANNEL, 2): "60",
        },
    )
    assert coordinator.get_exposure_compensation() is None
    assert coordinator.exposure_compensation_available() is False


def test_two_channels_use_their_own_profile_and_compensation():
    data = {
        "table.VideoInMode[0].Config[0]": "0",
        "table.VideoInMode[3].Config[0]": "1",
        _profile_key(0, 0): "25",
        _profile_key(0, 1): "35",
        _profile_key(3, 0): "65",
        _profile_key(3, 1): "75",
    }
    channel_zero = _coordinator(0, data)
    channel_three = _coordinator(3, data)

    assert channel_zero.get_exposure_compensation() == 25
    assert channel_three.get_exposure_compensation() == 75
    assert channel_zero.exposure_compensation_available() is True
    assert channel_three.exposure_compensation_available() is True


class _WriteClient:
    def __init__(self, mode, exposure):
        self.mode = mode
        self.exposure = exposure
        self.calls = []

    async def async_get_video_in_mode(self, fresh=False):
        self.calls.append(("profile", fresh))
        return self.mode

    async def async_get_exposure_anti_flicker_config(self, fresh=False):
        self.calls.append(("exposure", fresh))
        return self.exposure

    async def async_set_exposure_compensation(self, channel, profile, value):
        self.calls.append(("set", channel, profile, value))
        return {"result": True}


async def test_probe_uses_the_same_nonzero_channel_and_active_profile():
    exposure = {
        "table.VideoInExposure[0][0].Compensation": "20",
        _profile_key(CHANNEL, 0): "40",
        _profile_key(CHANNEL, 1): "60",
    }
    profile = {
        "table.VideoInMode[0].Config[0]": "0",
        PROFILE_KEY: "1",
    }
    coordinator = _coordinator(CHANNEL, {**profile, **exposure})
    coordinator.client = _WriteClient(
        profile,
        exposure,
    )

    assert await coordinator._async_probe_exposure_compensation() is True
    assert coordinator.supports_exposure_compensation() is True
    assert coordinator.get_exposure_compensation() == 60
    assert coordinator.exposure_compensation_available() is True
    assert coordinator.client.calls == [("profile", False), ("exposure", False)]


async def test_probe_does_not_inherit_channel_zero_or_another_profile():
    coordinator = _coordinator(CHANNEL, {})
    coordinator.client = _WriteClient(
        {
            "table.VideoInMode[0].Config[0]": "1",
            PROFILE_KEY: "1",
        },
        {
            "table.VideoInExposure[0][1].Compensation": "50",
            _profile_key(CHANNEL, 0): "40",
            _profile_key(CHANNEL, 2): "60",
        },
    )

    assert await coordinator._async_probe_exposure_compensation() is False
    assert coordinator.supports_exposure_compensation() is False


async def test_exposure_compensation_write_uses_fresh_profile_and_exposure_owner():
    coordinator = _coordinator(
        CHANNEL,
        {
            PROFILE_KEY: "0",
            _profile_key(CHANNEL, 0): "20",
        },
    )
    coordinator.client = _WriteClient(
        {
            PROFILE_KEY: "1",
            PROFILE_CONFIG_EX_KEY: "Night",
        },
        {
            "table.VideoInExposure[0][1].Compensation": "99",
            _profile_key(CHANNEL, 0): "40",
            _profile_key(CHANNEL, 1): "50",
        },
    )
    await coordinator.async_set_exposure_compensation(62)

    assert coordinator.client.calls == [
        ("profile", True),
        ("exposure", True),
        ("set", CHANNEL, 1, 62),
    ]


async def test_manual_mode_blocks_compensation_write_after_fresh_reads():
    coordinator = _coordinator(
        CHANNEL,
        {
            PROFILE_KEY: "1",
            MODE_KEY: "0",
            COMPENSATION_KEY: "50",
        },
        mode_supported=True,
    )
    coordinator.client = _WriteClient(
        {PROFILE_KEY: "1"},
        {MODE_KEY: "4", COMPENSATION_KEY: "50"},
    )

    with pytest.raises(ValueError, match="unavailable"):
        await coordinator.async_set_exposure_compensation(62)

    assert coordinator.client.calls == [("profile", True), ("exposure", True)]


async def test_warm_compatible_cache_cannot_allow_write_after_mode_changes(
    monkeypatch,
):
    client, device, calls, writes = _cached_client(monkeypatch)
    await client.async_get_exposure_anti_flicker_config()
    device["exposure"][MODE_KEY] = "4"

    coordinator = _coordinator(
        CHANNEL,
        {
            PROFILE_KEY: "1",
            MODE_KEY: "0",
            COMPENSATION_KEY: "50",
        },
        mode_supported=True,
    )
    coordinator.client = client

    with pytest.raises(ValueError, match="unavailable"):
        await coordinator.async_set_exposure_compensation(62)

    assert (
        calls.count("/cgi-bin/configManager.cgi?action=getConfig&name=VideoInExposure")
        == 2
    )
    assert writes == []


@pytest.mark.parametrize("mode_supported", [False, True])
async def test_unknown_fresh_mode_does_not_get_guessed_or_written(mode_supported):
    coordinator = _coordinator(
        CHANNEL,
        {
            PROFILE_KEY: "1",
            MODE_KEY: "0",
            COMPENSATION_KEY: "50",
        },
        mode_supported=mode_supported,
    )
    coordinator.client = _WriteClient(
        {PROFILE_KEY: "1"},
        {MODE_KEY: "unsupported", COMPENSATION_KEY: "50"},
    )

    with pytest.raises(ConnectionError, match="Mode could not be established"):
        await coordinator.async_set_exposure_compensation(62)

    assert coordinator.client.calls == [("profile", True), ("exposure", True)]


async def test_failed_fresh_exposure_read_has_no_stale_fallback_or_write():
    coordinator = _coordinator(
        CHANNEL,
        {
            PROFILE_KEY: "1",
            COMPENSATION_KEY: "50",
        },
    )

    class _FailingClient(_WriteClient):
        async def async_get_exposure_anti_flicker_config(self, fresh=False):
            self.calls.append(("exposure", fresh))
            raise ConnectionError("fresh read failed")

    coordinator.client = _FailingClient(
        {PROFILE_KEY: "1"},
        {COMPENSATION_KEY: "50"},
    )
    with pytest.raises(ConnectionError, match="fresh read failed"):
        await coordinator.async_set_exposure_compensation(62)
    assert coordinator.client.calls == [("profile", True), ("exposure", True)]


async def test_failed_fresh_read_does_not_fall_back_to_warm_exposure_cache(
    monkeypatch,
):
    client, device, calls, writes = _cached_client(monkeypatch)
    await client.async_get_exposure_anti_flicker_config()
    device["error"] = ConnectionError("fresh read failed")
    coordinator = _coordinator(
        CHANNEL,
        {
            PROFILE_KEY: "1",
            MODE_KEY: "0",
            COMPENSATION_KEY: "50",
        },
        mode_supported=True,
    )
    coordinator.client = client

    with pytest.raises(ConnectionError, match="fresh read failed"):
        await coordinator.async_set_exposure_compensation(62)

    assert (
        calls.count("/cgi-bin/configManager.cgi?action=getConfig&name=VideoInExposure")
        == 2
    )
    assert writes == []


async def test_failed_poll_carries_only_video_in_exposure_state():
    coordinator = _coordinator(CHANNEL, {})

    class _FailingClient:
        async def async_get_exposure_anti_flicker_config(self):
            raise ConnectionError("table unavailable")

    coordinator.client = _FailingClient()
    coordinator.data = {
        "table.VideoInMode[3].Config[0]": "1",
        "table.VideoInExposure[3][1].Compensation": "50",
        "table.VideoInExposure[3][1].AntiFlicker": "2",
        "table.VideoInExposure[3][1].Mode": "8",
        "table.VideoColor[3][0].Brightness": "40",
    }

    assert await coordinator._async_fetch_exposure_compensation() == {
        "table.VideoInExposure[3][1].Compensation": "50",
        "table.VideoInExposure[3][1].AntiFlicker": "2",
        "table.VideoInExposure[3][1].Mode": "8",
    }


def _client():
    client = object.__new__(DahuaClient)
    client._device = "10.0.0.5:80"
    client._username = "user"
    return client


def _cached_client(monkeypatch):
    client = _client()
    client._session = None
    client._refuse_if_closing = lambda: None
    profile_url = "/cgi-bin/configManager.cgi?action=getConfig&name=VideoInMode"
    exposure_url = "/cgi-bin/configManager.cgi?action=getConfig&name=VideoInExposure"
    device = {
        "exposure": {MODE_KEY: "0", COMPENSATION_KEY: "50"},
    }
    calls = []
    writes = []
    monkeypatch.setattr(client_module, "_HOST_CACHE", {})

    async def request(url, _verify_ok=False):
        calls.append(url)
        if url == profile_url:
            return {PROFILE_KEY: "1"}
        if url == exposure_url:
            if "error" in device:
                raise device["error"]
            return dict(device["exposure"])
        pytest.fail(f"Unexpected request: {url}")

    async def set_compensation(channel, profile, value):
        writes.append((channel, profile, value))
        return "OK"

    client._request = request
    client.async_set_exposure_compensation = set_compensation
    return client, device, calls, writes


async def test_client_writes_only_the_exact_compensation_field():
    client = _client()
    calls = []

    async def get(url):
        calls.append(url)
        return "OK"

    client.get = get
    await client.async_set_exposure_compensation(CHANNEL, 2, 62)

    assert calls == [
        (
            "/cgi-bin/configManager.cgi?action=setConfig"
            "&VideoInExposure[3][2].Compensation=62"
        )
    ]
    assert "AntiFlicker" not in calls[0]
    assert "setTemporaryConfig" not in calls[0]


async def test_client_rpc2_fallback_uses_the_same_exact_field():
    client = _client()
    fallback = []

    async def get(_url):
        raise ClientResponseError(None, (), status=404)

    async def set_field(name, value):
        fallback.append((name, value))
        return {"result": True}

    client.get = get
    client._rpc2_set_config_field = set_field
    await client.async_set_exposure_compensation(CHANNEL, 2, 62)

    assert fallback == [("VideoInExposure[3][2].Compensation", 62)]


async def test_client_propagates_non_absent_cgi_errors():
    client = _client()
    error = ClientResponseError(None, (), status=403)

    async def get(_url):
        raise error

    async def fallback(*_args):
        pytest.fail("RPC2 fallback must only follow a missing CGI endpoint")

    client.get = get
    client._rpc2_set_config_field = fallback
    with pytest.raises(ClientResponseError) as caught:
        await client.async_set_exposure_compensation(CHANNEL, 2, 62)
    assert caught.value is error


async def test_fresh_reads_bypass_warm_profile_and_exposure_cache(monkeypatch):
    client = _client()
    profile_url = "/cgi-bin/configManager.cgi?action=getConfig&name=VideoInMode"
    exposure_url = "/cgi-bin/configManager.cgi?action=getConfig&name=VideoInExposure"
    monkeypatch.setattr(
        client_module,
        "_HOST_CACHE",
        {
            (client._device, client._username, profile_url): "warm profile",
            (client._device, client._username, exposure_url): "warm exposure",
        },
    )
    calls = []

    async def get(url):
        calls.append(url)
        assert (
            client._device,
            client._username,
            url,
        ) not in client_module._HOST_CACHE
        return {}

    client.get = get
    await client.async_get_video_in_mode(fresh=True)
    await client.async_get_exposure_anti_flicker_config(fresh=True)

    assert calls == [profile_url, exposure_url]


@pytest.mark.parametrize(
    "arguments",
    [
        (-1, 2, 62),
        (True, 2, 62),
        (CHANNEL, -1, 62),
        (CHANNEL, True, 62),
        (CHANNEL, 2, True),
        (CHANNEL, 2, 62.0),
        (CHANNEL, 2, -1),
        (CHANNEL, 2, 101),
    ],
)
async def test_client_rejects_invalid_values_without_a_request(arguments):
    client = _client()
    client.get = pytest.fail
    client._rpc2_set_config_field = pytest.fail
    with pytest.raises(ValueError):
        await client.async_set_exposure_compensation(*arguments)


class _EntityCoordinator:
    def __init__(self, error=None):
        self.calls = []
        self.error = error
        self.refreshed = 0

    async def async_set_exposure_compensation(self, value):
        self.calls.append(value)
        if self.error is not None:
            raise self.error

    async def async_refresh(self):
        self.refreshed += 1


def _entity(coordinator):
    entity = object.__new__(DahuaExposureCompensationNumber)
    entity._coordinator = coordinator
    return entity


@pytest.mark.parametrize("value", [1.5, True, False])
async def test_number_entity_rejects_invalid_values_without_call_or_refresh(value):
    coordinator = _EntityCoordinator()
    with pytest.raises(ValueError):
        await _entity(coordinator).async_set_native_value(value)
    assert coordinator.calls == []
    assert coordinator.refreshed == 0


async def test_number_entity_accepts_integral_float_and_refreshes_once():
    coordinator = _EntityCoordinator()
    await _entity(coordinator).async_set_native_value(62.0)
    assert coordinator.calls == [62]
    assert coordinator.refreshed == 1


async def test_number_entity_does_not_refresh_after_a_failed_write():
    coordinator = _EntityCoordinator(error=ConnectionError("write failed"))
    with pytest.raises(ConnectionError, match="write failed"):
        await _entity(coordinator).async_set_native_value(62.0)
    assert coordinator.calls == [62]
    assert coordinator.refreshed == 0
