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
    client._address = "10.0.0.5"
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


# --- a 403 is remembered, until a reload -----------------------------------------


async def test_a_403_is_not_asked_again():
    """#1235: refused on every poll, on both cameras, for days."""
    client = _client(_refused(403))

    await client.async_get_config_motion_detection()
    again = await client.async_get_config_motion_detection()

    assert client.urls == [MOTION_URL]
    assert again == {}
    assert _switch(dict(again)).is_on is None


async def test_a_reload_asks_again():
    """Each channel builds its own client and a reload builds new ones, so an
    account given more rights gets its setting back without a restart."""
    refused = _client(_refused(403))
    await refused.async_get_config_motion_detection()

    after_reload = _client({"table.MotionDetect[0].Enable": "true"})
    answer = await after_reload.async_get_config_motion_detection()

    assert after_reload.urls == [MOTION_URL]
    assert _switch(dict(answer)).is_on is True


@pytest.mark.parametrize("status", [400, 401, 500])
async def test_any_other_refusal_is_asked_again(status):
    """Only the 403 #1235 measured. A 401 is the credentials, which reauth
    handles, and a 500 can be a device having a bad moment."""
    client = _client(_refused(status))

    await client.async_get_config_motion_detection()
    await client.async_get_config_motion_detection()

    assert client.urls == [MOTION_URL, MOTION_URL]


async def test_the_refusal_is_said_once(caplog):
    caplog.set_level("INFO", logger="custom_components.dahua")
    client = _client(_refused(403))

    for _ in range(3):
        await client.async_get_config_motion_detection()

    said = [r for r in caplog.records if "motion detection setting" in r.getMessage()]
    assert len(said) == 1
    assert "10.0.0.5" in said[0].getMessage()


# --- SmartMotionDetect: a refused table is not taken as support ---------------------


def _probing(answer, channel=0):
    coordinator = object.__new__(DahuaDataUpdateCoordinator)
    coordinator._channel = channel
    coordinator._address = "10.0.0.5"
    coordinator._supports_smart_motion_detection = False
    coordinator._probe_refusals = {}
    coordinator._capability_retries = {}
    coordinator.data = {}
    coordinator.model = "DH-C4K-P"

    async def async_get_smart_motion_detection():
        return dict(answer)

    coordinator.client = SimpleNamespace(
        async_get_smart_motion_detection=async_get_smart_motion_detection
    )
    return coordinator


async def test_a_refused_smart_motion_table_is_not_polled():
    """The refusal reaches the probe as {}. It used to set the flag the poll is
    gated on anyway, so the table was asked for every poll in #1235."""
    coordinator = _probing({})

    assert await coordinator._async_probe_smart_motion_detect() is False
    assert coordinator._supports_smart_motion_detection is False


async def test_a_table_with_another_channels_row_is_still_polled():
    """Unchanged: a recorder's table is host-wide and sparse, and whether this
    channel's row is there is decided per channel."""
    coordinator = _probing({"table.SmartMotionDetect[3].Enable": "true"}, channel=0)

    assert await coordinator._async_probe_smart_motion_detect() is False
    assert coordinator._supports_smart_motion_detection is True


async def test_a_table_with_this_channels_row_is_supported():
    coordinator = _probing({"table.SmartMotionDetect[0].Enable": "false"})

    assert await coordinator._async_probe_smart_motion_detect() is True
    assert coordinator._supports_smart_motion_detection is True
