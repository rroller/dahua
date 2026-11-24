"""A motion detection setting the device would not give reads unknown, not off (#1235).

The reporter's account got 403 on `MotionDetect` on every poll, on both cameras,
while the cameras pushed motion events all along. The Motion Detection switch
showed "off". Nothing had read "off": `async_get_config` turns the refusal into
no keys at all, and the reader treated a missing key as false.

These go through the real client method, the real coordinator readers and the
real switch, so a double can't agree with itself.
"""

from types import SimpleNamespace

import aiohttp
import pytest

from custom_components.dahua import DahuaDataUpdateCoordinator
from custom_components.dahua.client import DahuaClient
from custom_components.dahua.switch import DahuaMotionDetectionBinarySwitch

MOTION_URL = "/cgi-bin/configManager.cgi?action=getConfig&name=MotionDetect"


def _client(answer):
    """A client whose one request answers `answer`, or raises it."""
    client = object.__new__(DahuaClient)
    client.urls = []

    async def get(url, *args, **kwargs):
        client.urls.append(url)
        if isinstance(answer, Exception):
            raise answer
        return answer

    client.get = get
    return client


def _switch(data, channel=0):
    coordinator = object.__new__(DahuaDataUpdateCoordinator)
    coordinator.data = data
    coordinator._channel = channel
    switch = object.__new__(DahuaMotionDetectionBinarySwitch)
    switch._coordinator = coordinator
    switch.coordinator = coordinator
    return switch


def _refused(status):
    return aiohttp.ClientResponseError(
        SimpleNamespace(real_url=MOTION_URL), (), status=status, message="Forbidden"
    )


@pytest.mark.parametrize("status", [403, 400, 500])
async def test_a_refused_motion_table_reads_unknown(status):
    client = _client(_refused(status))

    answer = await client.async_get_config_motion_detection()

    assert client.urls == [MOTION_URL]
    assert answer == {}
    assert _switch(dict(answer)).is_on is None


@pytest.mark.parametrize("value, expected", [("true", True), ("false", False)])
async def test_an_answered_motion_table_reads_what_it_says(value, expected):
    client = _client({"table.MotionDetect[0].Enable": value})

    answer = await client.async_get_config_motion_detection()

    assert _switch(dict(answer)).is_on is expected


def test_another_channels_answer_is_not_this_channels():
    """On a recorder the table is shared, so channel 0's row says nothing about
    channel 2."""
    data = {"table.MotionDetect[0].Enable": "false"}

    assert _switch(data, channel=2).is_on is None
    assert _switch(data, channel=0).is_on is False


def test_the_reader_the_camera_uses_stays_a_plain_bool():
    """The camera entity and the event stream read this as yes or no."""
    coordinator = _switch({}, channel=0)._coordinator

    assert coordinator.motion_detection_was_read() is False
    assert coordinator.is_motion_detection_enabled() is False
