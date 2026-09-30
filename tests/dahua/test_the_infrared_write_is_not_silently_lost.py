"""Pressing the infrared toggle on a recorder did nothing, silently, two ways.

Measured on a DHI-NVR5464-16P-EI carrying twelve channels, from the log of a real
user pressing the button:

    00:03:48 Writing to 192.168.0.213: setConfig&Lighting[6][0].Mode=Manual&...
    00:03:48 ClientError ...: HTTP 403 Forbidden          <- channel 6, three times
    00:04:41 ClientError ...: HTTP 403 Forbidden          <- channel 8
    00:05:21 Writing ...Lighting[3][0].Mode=Manual        <- channel 3, four times,
                                                             HTTP 200 every time

Two different failures, neither of which reached the user:

1. **Refused.** Two of ten channels answered `403`. The raw
   `aiohttp.ClientResponseError` escaped `async_turn_on`, so the frontend showed
   `403, message='Forbidden', url='http://.../configManager.cgi?action=setConfig
   &Lighting%5B6%5D%5B0%5D.Mode=Manual...'` -- a request, not a reason -- and the
   integration logged the whole thing at **debug**, so with default logging
   nothing reached the log at all.

2. **Accepted and ignored.** Channel 3 answered `200` to all four writes and
   `Lighting[3][0].Mode` was still `Auto` afterwards. The refresh then showed the
   toggle springing back with nothing said about why.

The status code says the request was accepted, not that anything changed, so
reading the mode back is the only way to tell those apart from a write that
worked. That read is judged on the **mode**, not on `is_on`: an ignored turn-off
leaves the channel on `Auto`, which is not `Manual` either, so `is_on` would
agree with the write that failed.
"""

import asyncio

import aiohttp
import pytest

from homeassistant.exceptions import HomeAssistantError

from custom_components.dahua import dahua_utils
from custom_components.dahua.light import DahuaInfraredLight

from .test_light import _Coordinator, _light

ATTR_BRIGHTNESS = "brightness"


def _response_error(status, message):
    """An aiohttp error shaped the way the device's refusal really arrives.

    request_info is not optional in practice: aiohttp reads
    `request_info.real_url` while formatting one, so a None here raises from
    inside the error's own __str__ and the test measures the stand-in.
    """
    return aiohttp.ClientResponseError(
        aiohttp.RequestInfo(
            url="http://recorder/cgi-bin/configManager.cgi",
            method="GET",
            headers=aiohttp.typedefs.CIMultiDict(),
            real_url="http://recorder/cgi-bin/configManager.cgi",
        ),
        (),
        status=status,
        message=message,
    )


# --- a refusal reaches the user ------------------------------------------------

@pytest.mark.parametrize("enabled", [True, False])
async def test_a_refused_write_is_reported_not_swallowed(enabled):
    coordinator = _Coordinator(channel=6)
    coordinator.client.v1_refuses = _response_error(403, "Forbidden")
    light = _light(DahuaInfraredLight, coordinator)

    with pytest.raises(HomeAssistantError) as caught:
        if enabled:
            await light.async_turn_on()
        else:
            await light.async_turn_off()

    assert caught.value.translation_key == "infrared_write_refused"
    assert caught.value.translation_placeholders["reason"] == "HTTP 403 Forbidden"
    assert caught.value.translation_placeholders["device"] == "Front Door"
    # The write was attempted. A guard that never called the device would also
    # raise, and would pass this test without the fix being the reason.
    assert coordinator.client.v1 == [(6, enabled, 100, "1")]


@pytest.mark.parametrize("error", [
    asyncio.TimeoutError(),
    aiohttp.ClientConnectionError("connection reset"),
])
async def test_a_device_that_never_answers_is_reported_too(error):
    """A refusal is not the only way this fails, and a timeout carries no
    message at all -- formatting one straight into a card gives a blank where
    the cause should be."""
    coordinator = _Coordinator()
    coordinator.client.v1_refuses = error
    light = _light(DahuaInfraredLight, coordinator)

    with pytest.raises(HomeAssistantError) as caught:
        await light.async_turn_on()

    assert caught.value.translation_key == "infrared_write_refused"
    assert caught.value.translation_placeholders["reason"]


# --- a 200 that changed nothing is not a success ------------------------------

@pytest.mark.parametrize("enabled,wanted", [(True, "Manual"), (False, "Off")])
async def test_a_write_the_device_ignored_is_reported(enabled, wanted):
    """Channel 3's case: 200 four times over, and the mode never moved."""
    coordinator = _Coordinator(channel=3)
    coordinator.client.v1_ignores = True
    coordinator.client.infrared_mode = "Auto"
    light = _light(DahuaInfraredLight, coordinator)

    with pytest.raises(HomeAssistantError) as caught:
        if enabled:
            await light.async_turn_on()
        else:
            await light.async_turn_off()

    assert caught.value.translation_key == "infrared_write_ignored"
    placeholders = caught.value.translation_placeholders
    assert placeholders["wanted"] == wanted
    assert placeholders["reported"] == "Auto"
    # The check has to happen after a refresh, or it reads the state from before
    # the write and reports every successful write as ignored.
    assert coordinator.refreshed == 1


async def test_an_ignored_turn_off_is_caught_even_though_is_on_agrees():
    """The reason this is judged on the mode. `Auto` is not `Manual`, so `is_on`
    is False either way and cannot tell a working turn-off from a lost one."""
    coordinator = _Coordinator(channel=3)
    coordinator.client.v1_ignores = True
    coordinator.client.infrared_mode = "Auto"
    coordinator.infrared_on = False          # what is_on would report
    light = _light(DahuaInfraredLight, coordinator)

    assert light.is_on is False, "the state the old code would have accepted"

    with pytest.raises(HomeAssistantError) as caught:
        await light.async_turn_off()

    assert caught.value.translation_key == "infrared_write_ignored"


@pytest.mark.parametrize("enabled,mode", [(True, "Manual"), (False, "Off")])
async def test_a_write_the_device_took_raises_nothing(enabled, mode):
    coordinator = _Coordinator(channel=3)
    light = _light(DahuaInfraredLight, coordinator)

    if enabled:
        await light.async_turn_on(**{ATTR_BRIGHTNESS: 255})
    else:
        await light.async_turn_off()

    assert coordinator.client.infrared_mode == mode
    assert coordinator.refreshed == 1


async def test_a_channel_reporting_no_lighting_at_all_is_not_an_ignored_write():
    """An empty mode is a channel with no Lighting row, not a device that
    refused. There is nothing to compare against, and raising here would put an
    error on every press for a device the entity should not exist on."""
    coordinator = _Coordinator()
    coordinator.client.v1_ignores = True
    coordinator.client.infrared_mode = ""
    light = _light(DahuaInfraredLight, coordinator)

    await light.async_turn_on()

    assert coordinator.client.v1


async def test_a_mode_the_device_chose_itself_is_reported_as_what_it_says():
    """Two of this recorder's fifteen channels report `ZoomPrio`, which the
    integration never writes. It has to survive being read back rather than
    being flattened into on or off, or the message names the wrong thing."""
    coordinator = _Coordinator()
    coordinator.client.v1_ignores = True
    coordinator.client.infrared_mode = "ZoomPrio"
    light = _light(DahuaInfraredLight, coordinator)

    with pytest.raises(HomeAssistantError) as caught:
        await light.async_turn_on()

    assert caught.value.translation_placeholders["reported"] == "ZoomPrio"


# --- the phrase the user reads ------------------------------------------------

def test_a_status_is_named_rather_than_the_request_url():
    """`str()` on the real error is the whole setConfig URL. That is what used
    to reach the frontend."""
    error = _response_error(403, "Forbidden")
    assert "Lighting" not in dahua_utils.describe_write_refusal(error)
    assert dahua_utils.describe_write_refusal(error) == "HTTP 403 Forbidden"


def test_the_other_refusal_this_recorder_gives_is_named_too():
    assert dahua_utils.describe_write_refusal(
        _response_error(400, "Bad Request")) == "HTTP 400 Bad Request"


def test_an_exception_with_no_message_falls_back_to_its_type():
    """`str(asyncio.TimeoutError())` is empty, which is the blank this avoids."""
    assert dahua_utils.describe_write_refusal(asyncio.TimeoutError()) == "TimeoutError"


def test_a_message_is_used_when_there_is_no_status():
    assert dahua_utils.describe_write_refusal(
        ConnectionError("session is out of date!")) == "session is out of date!"


def test_a_status_that_is_not_a_number_is_not_treated_as_one():
    """Something non-aiohttp can carry a `status` attribute meaning anything.
    Only an int is an HTTP status, and True is an int in Python."""

    class _Odd(Exception):
        status = "weird"

    assert dahua_utils.describe_write_refusal(_Odd("the real reason")) \
        == "the real reason"

    class _Boolish(Exception):
        status = True

    assert dahua_utils.describe_write_refusal(_Boolish("still the reason")) \
        == "still the reason"
