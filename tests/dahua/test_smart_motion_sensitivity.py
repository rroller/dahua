"""Smart motion detection sensitivity as a select (Reolink/Tapo parity).

The enable switch could turn smart motion on and off; nothing tuned how
sensitive it was. The device stores a word in the same per-channel
SmartMotionDetect row the switch writes -- measured Sensitivity=Middle on a
DHI-NVR5464-16P-EI, and the rows are sparse there (channels 1 and 11 carried a
row, the rest were absent) -- so the write is channel-scoped for the same
reason the enable write is, and dodges the whole-table size ceiling.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import pytest

from custom_components.dahua import DahuaDataUpdateCoordinator
from custom_components.dahua.client import DahuaClient
from custom_components.dahua.select import DahuaSmartMotionSensitivitySelect

# One recorder's sparse SmartMotionDetect table, flattened as the poll stores it.
NVR = {
    "table.SmartMotionDetect[1].Sensitivity": "Middle",
    "table.SmartMotionDetect[11].Sensitivity": "High",
}


# --- the coordinator getter -------------------------------------------------


def _coordinator(channel, data):
    c = object.__new__(DahuaDataUpdateCoordinator)
    c._channel = channel
    c.data = data
    return c


def test_the_getter_reads_this_channels_row():
    assert _coordinator(1, NVR).get_smart_motion_sensitivity() == "Middle"
    assert _coordinator(11, NVR).get_smart_motion_sensitivity() == "High"


def test_a_channel_without_a_row_reads_nothing():
    """The rows are sparse, so a channel the device does not list is None, not a
    value borrowed from row 0."""
    assert _coordinator(0, NVR).get_smart_motion_sensitivity() is None
    assert _coordinator(2, {}).get_smart_motion_sensitivity() is None


# --- the select entity ------------------------------------------------------


def _select(value=None, channel=0):
    """The entity without Home Assistant's plumbing, as the other select tests do."""
    s = object.__new__(DahuaSmartMotionSensitivitySelect)
    s._coordinator = SimpleNamespace(
        get_smart_motion_sensitivity=lambda: value,
        get_channel=lambda: channel,
        client=SimpleNamespace(async_set_smart_motion_sensitivity=AsyncMock()),
        async_refresh=AsyncMock(),
    )
    return s


def test_current_option_is_the_device_word():
    assert _select("Middle").current_option == "Middle"
    assert _select("High").current_option == "High"


def test_a_value_the_select_does_not_offer_is_unknown():
    """A camera can report a mode of its own; Home Assistant rejects a
    current_option outside options, so it shows as unknown rather than as a
    sensitivity the camera is not in."""
    assert _select("Potato").current_option is None
    assert _select(None).current_option is None


async def test_selecting_writes_this_channel_and_refreshes():
    s = _select(value="Low", channel=3)

    await s.async_select_option("High")

    s._coordinator.client.async_set_smart_motion_sensitivity.assert_awaited_once_with(
        3, "High"
    )
    s._coordinator.async_refresh.assert_awaited_once()


async def test_an_option_the_entity_does_not_offer_is_not_written():
    s = _select()

    await s.async_select_option("Potato")

    s._coordinator.client.async_set_smart_motion_sensitivity.assert_not_awaited()
    s._coordinator.async_refresh.assert_not_awaited()


# --- the write method -------------------------------------------------------


class _RecordingClient(DahuaClient):
    """The real write, with get() and the RPC2 fallback replaced by recorders."""

    def __init__(self, status=None):
        self.urls = []
        self.rpc2_calls = []
        self._status = status
        self._address = "10.0.0.1"

    async def get(self, url, verify_ok=False):
        self.urls.append(url)
        if self._status is not None:
            raise aiohttp.ClientResponseError(
                None, None, status=self._status, message="x"
            )
        return "OK"

    async def _rpc2_set_config_value(self, name, channel, key, value):
        self.rpc2_calls.append((name, channel, key, value))
        return {"result": True}


async def test_the_write_builds_the_channel_scoped_url():
    c = _RecordingClient()

    await c.async_set_smart_motion_sensitivity(3, "High")

    assert c.urls == [
        "/cgi-bin/configManager.cgi?action=setConfig"
        "&SmartMotionDetect[3].Sensitivity=High"
    ]
    assert c.rpc2_calls == []


async def test_an_absent_cgi_falls_back_to_rpc2():
    # 404 is in CONFIG_CGI_ABSENT: an SL300 serves the table but answers 404 on
    # the CGI write path, the same device the enable write handles this way.
    c = _RecordingClient(status=404)

    await c.async_set_smart_motion_sensitivity(1, "Low")

    assert c.rpc2_calls == [("SmartMotionDetect", 1, "Sensitivity", "Low")]


async def test_a_real_error_is_not_swallowed():
    c = _RecordingClient(status=403)

    with pytest.raises(aiohttp.ClientResponseError):
        await c.async_set_smart_motion_sensitivity(1, "Low")

    assert c.rpc2_calls == []
