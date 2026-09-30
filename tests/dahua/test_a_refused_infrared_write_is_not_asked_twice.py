"""A control the device will never accept stops being offered.

Measured on a DHI-NVR5464-16P-EI, with a verified restore, after the earlier
guess was corrected by actually writing to it:

    CGI  setConfig&Lighting[3][0].Mode=Manual  HTTP 403  "Authority:check failure."
    CGI  setConfig&Lighting[6][0].Mode=Manual  HTTP 403  "Authority:check failure."
    RPC2 configManager.setConfig name=Lighting errCode 285278249, same message

Every channel, both transports. So on that recorder the infrared write can never
succeed, and the mode select is the shape #525 was about: a dropdown that was
never going to work. Asking again on every press spends a request to be told the
same thing and leaves somebody pressing a button that looks like it might work.

The split is deliberate:

- **the select becomes unavailable**, because it exists only to write
- **the light stays available**, because `mode` and `brightness_level` are read
  off the same table and are correct. `Auto` at level 50 is useful to see even
  where it cannot be changed, and taking the entity away would lose it.

What counts as a refusal is `403` on CGI and `285278249` on RPC2 -- the device
answering and declining. A timeout or a dropped connection says nothing about
whether the write is possible, and writing a channel off for one would take a
working control away from somebody whose camera was merely busy. `401` is left out
too: that is the credentials, which reauth exists for.

Not remembered across a restart, and forgotten when the entry unloads, so a
permissions change on the device costs one refusal to discover rather than being
invisible until somebody thinks to reload.
"""

import asyncio

import aiohttp
import pytest

from homeassistant.exceptions import HomeAssistantError

from custom_components.dahua import infrared
from custom_components.dahua.infrared import (
    forget_refused_infrared_writes,
    infrared_write_is_refused,
    refusal_is_outright,
)
from custom_components.dahua.light import DahuaInfraredLight
from custom_components.dahua.rpc2 import Rpc2MethodRefused
from custom_components.dahua.select import DahuaInfraredModeSelect

from .test_light import _Coordinator, _light


def _response_error(status, message="Forbidden"):
    """Shaped the way the refusal really arrives.

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


def _select(coordinator):
    entity = object.__new__(DahuaInfraredModeSelect)
    entity._coordinator = coordinator
    entity.coordinator = coordinator
    return entity


# --- which failures are a refusal ---------------------------------------------

def test_the_status_this_recorder_answers_is_a_refusal():
    assert refusal_is_outright(_response_error(403)) is True


def test_the_rpc2_code_for_the_same_answer_is_a_refusal():
    assert refusal_is_outright(
        Rpc2MethodRefused("no", code=285278249,
                          message="Authority:check failure.")) is True


@pytest.mark.parametrize("error", [
    _response_error(500, "Internal Server Error"),
    _response_error(400, "Bad Request"),
    _response_error(401, "Unauthorized"),
    asyncio.TimeoutError(),
    aiohttp.ClientConnectionError("reset"),
    Rpc2MethodRefused("stale", code=287637504, message="session is out of date!"),
])
def test_everything_else_is_not_a_refusal(error):
    """A device that could not be reached, or that said something else, has not
    said the write is impossible. 401 is the credentials -- reauth's job, and a
    later write may well succeed. 287637504 is an expired session, which the
    shared-login retry already handles."""
    assert refusal_is_outright(error) is False


# --- learning it once ---------------------------------------------------------

async def test_a_refused_write_is_remembered_and_not_sent_again():
    coordinator = _Coordinator(channel=3)
    coordinator.client.v1_refuses = _response_error(403)
    light = _light(DahuaInfraredLight, coordinator)

    with pytest.raises(HomeAssistantError) as first:
        await light.async_turn_on()
    assert first.value.translation_key == "infrared_write_refused"
    assert len(coordinator.client.v1) == 1, "the first press must reach the device"
    assert infrared_write_is_refused(coordinator)

    with pytest.raises(HomeAssistantError) as second:
        await light.async_turn_on()

    assert second.value.translation_key == "infrared_write_already_refused"
    assert len(coordinator.client.v1) == 1, (
        "the second press was sent to a device that had already said no")


async def test_a_failure_that_is_not_a_refusal_is_asked_again():
    """The regression that would hurt most: one timeout must not disable a
    working control for the life of the process."""
    coordinator = _Coordinator(channel=3)
    coordinator.client.v1_refuses = asyncio.TimeoutError()
    light = _light(DahuaInfraredLight, coordinator)

    with pytest.raises(HomeAssistantError):
        await light.async_turn_on()
    assert not infrared_write_is_refused(coordinator)

    coordinator.client.v1_refuses = None
    await light.async_turn_on()

    assert len(coordinator.client.v1) == 2
    assert coordinator.client.infrared_mode == "Manual"


async def test_one_channel_refusing_does_not_silence_another():
    """Two channels of the same recorder. On the measured device both refuse, but
    that has to be learnt per channel: a camera behind one channel of an NVR is
    not the camera behind another."""
    refused = _Coordinator(channel=3)
    refused.client.v1_refuses = _response_error(403)
    with pytest.raises(HomeAssistantError):
        await _light(DahuaInfraredLight, refused).async_turn_on()

    other = _Coordinator(channel=6)
    assert not infrared_write_is_refused(other)

    await _light(DahuaInfraredLight, other).async_turn_on()
    assert other.client.infrared_mode == "Manual"


def test_what_was_learnt_is_forgotten_for_one_channel_at_a_time():
    """Unload forgets per channel, so reloading one recorder does not make every
    other host pay a refusal again."""
    one = _Coordinator(channel=3)
    another = _Coordinator(channel=6)
    infrared._WRITE_REFUSED.add(infrared._key(one))
    infrared._WRITE_REFUSED.add(infrared._key(another))

    forget_refused_infrared_writes(one)

    assert not infrared_write_is_refused(one)
    assert infrared_write_is_refused(another), "the other channel was forgotten too"


# --- what the two entities do about it ----------------------------------------

async def test_the_select_goes_unavailable_and_the_light_does_not():
    """The split this change is about. The select exists only to write; the
    light's mode and level are read off the same table and stay correct."""
    coordinator = _Coordinator(channel=3)
    coordinator.client.infrared_mode = "Auto"
    coordinator.infrared_level = 50
    select = _select(coordinator)
    light = _light(DahuaInfraredLight, coordinator)

    assert select.available is True

    coordinator.client.v1_refuses = _response_error(403)
    with pytest.raises(HomeAssistantError):
        await light.async_turn_on()

    assert select.available is False
    attributes = light.extra_state_attributes
    assert (attributes["mode"], attributes["brightness_level"]) == ("Auto", 50)


async def test_the_select_refusing_teaches_the_light_too():
    """One store, whichever entity met the refusal. Pressing the select and then
    the light must not cost two requests to learn the same thing."""
    coordinator = _Coordinator(channel=3)
    coordinator.client.v1_refuses = _response_error(403)

    with pytest.raises(HomeAssistantError):
        await _select(coordinator).async_select_option("manual")
    assert len(coordinator.client.v1) == 1

    with pytest.raises(HomeAssistantError) as caught:
        await _light(DahuaInfraredLight, coordinator).async_turn_off()

    assert caught.value.translation_key == "infrared_write_already_refused"
    assert len(coordinator.client.v1) == 1


async def test_an_unavailable_select_still_reports_the_mode_it_last_read():
    """`available` is about acting, not about knowing. Home Assistant shows an
    unavailable entity's state as unavailable, but the property must not start
    lying or returning None -- the diagnostics dump reads it."""
    coordinator = _Coordinator(channel=3)
    coordinator.client.infrared_mode = "Auto"
    coordinator.client.v1_refuses = _response_error(403)
    select = _select(coordinator)

    with pytest.raises(HomeAssistantError):
        await select.async_select_option("manual")

    assert select.available is False
    assert select.current_option == "auto"
