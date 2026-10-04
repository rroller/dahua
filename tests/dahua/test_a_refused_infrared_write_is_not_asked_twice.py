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

from custom_components.dahua import refusals
from custom_components.dahua.infrared import (
    CONTROL,
    forget_refused_infrared_writes,
    infrared_write_is_refused,
)
from custom_components.dahua.refusals import refusal_is_outright
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


# Which failures count as a refusal is decided in refusals.py and tested in
# test_what_counts_as_a_device_refusing_outright.py, against the real module and
# with a mutation sweep. One assertion stays here, so this file does not pass if
# the infrared path stops agreeing with that decision.


def test_the_status_this_recorder_answers_is_still_a_refusal():
    assert refusal_is_outright(_response_error(403)) is True


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
    assert (
        len(coordinator.client.v1) == 1
    ), "the second press was sent to a device that had already said no"


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
    refusals.remember(one, CONTROL, "refused")
    refusals.remember(another, CONTROL, "refused")

    forget_refused_infrared_writes(one)

    assert not infrared_write_is_refused(one)
    assert infrared_write_is_refused(another), "the other channel was forgotten too"


# --- and the fallback to Lighting_V2, which is the point of the whole store ----
#
# v1 is tried first for every device. Only an outright refusal reaches v2, and only
# when the channel has a row there. See
# test_which_lighting_table_drives_infrared.py for why the rule is not "v2 wherever
# a row exists": #647 has a directly connected camera reporting three v2 rows whose
# v1 table presumably works, and that issue is literally "the config changes and the
# LEDs do not".

V2_ROW = ("0", 0, "NearLight")


async def test_v1_is_tried_first_even_when_a_v2_row_exists():
    coordinator = _Coordinator(channel=11)
    coordinator.infrared_v2_row = V2_ROW
    light = _light(DahuaInfraredLight, coordinator)

    await light.async_turn_on()

    assert len(coordinator.client.v1) == 1, "v1 was not tried"
    assert coordinator.client.v2_modes == [], "v2 was used while v1 was working"


async def test_a_refused_v1_falls_through_to_v2_in_the_same_press():
    """The recorder's case. Making the user press twice to discover the fallback
    would read as the first press having done nothing."""
    coordinator = _Coordinator(channel=11)
    coordinator.infrared_v2_row = V2_ROW
    coordinator.client.v1_refuses = _response_error(403)
    light = _light(DahuaInfraredLight, coordinator)

    await light.async_turn_on()

    assert len(coordinator.client.v1) == 1
    assert len(coordinator.client.v2_modes) == 1, "the fallback was not taken"
    channel, mode, _brightness, profile, index, bank = coordinator.client.v2_modes[0]
    assert (channel, mode, profile, index, bank) == (11, "Manual", "0", 0, "NearLight")


async def test_once_v1_is_known_refused_it_is_not_tried_again():
    coordinator = _Coordinator(channel=11)
    coordinator.infrared_v2_row = V2_ROW
    coordinator.client.v1_refuses = _response_error(403)
    light = _light(DahuaInfraredLight, coordinator)

    await light.async_turn_on()
    await light.async_turn_off()

    assert len(coordinator.client.v1) == 1, "v1 was asked again after refusing"
    assert len(coordinator.client.v2_modes) == 2


async def test_a_refused_v1_with_no_v2_row_still_reports_and_stops():
    """Thirteen of that recorder's fifteen channels."""
    coordinator = _Coordinator(channel=3)
    coordinator.infrared_v2_row = None
    coordinator.client.v1_refuses = _response_error(403)
    light = _light(DahuaInfraredLight, coordinator)

    with pytest.raises(HomeAssistantError) as first:
        await light.async_turn_on()
    assert first.value.translation_key == "infrared_write_refused"

    with pytest.raises(HomeAssistantError) as second:
        await light.async_turn_on()
    assert second.value.translation_key == "infrared_write_already_refused"
    assert len(coordinator.client.v1) == 1


async def test_both_paths_refusing_is_what_takes_the_control_away():
    """A channel with a v2 row is not "refused" while the fallback is untried --
    reporting it so would remove a control that still had somewhere to go."""
    coordinator = _Coordinator(channel=11)
    coordinator.infrared_v2_row = V2_ROW
    coordinator.client.v1_refuses = _response_error(403)
    coordinator.client.v2_refuses = _response_error(403)
    light = _light(DahuaInfraredLight, coordinator)

    with pytest.raises(HomeAssistantError):
        await light.async_turn_on()

    assert infrared_write_is_refused(coordinator)
    assert _select(coordinator).available is False

    with pytest.raises(HomeAssistantError) as again:
        await light.async_turn_on()
    assert again.value.translation_key == "infrared_write_already_refused"
    assert len(coordinator.client.v1) == 1
    assert len(coordinator.client.v2_modes) == 1


async def test_a_timeout_on_v1_does_not_burn_the_fallback():
    """The regression that would hurt most. One blip must not move a camera onto a
    table it may not honour, and must not spend the v2 attempt either."""
    coordinator = _Coordinator(channel=11)
    coordinator.infrared_v2_row = V2_ROW
    coordinator.client.v1_refuses = asyncio.TimeoutError()
    light = _light(DahuaInfraredLight, coordinator)

    with pytest.raises(HomeAssistantError):
        await light.async_turn_on()

    assert coordinator.client.v2_modes == [], "a timeout reached the fallback"
    assert not infrared_write_is_refused(coordinator)

    coordinator.client.v1_refuses = None
    await light.async_turn_on()
    assert len(coordinator.client.v1) == 2
    assert coordinator.client.v2_modes == []


async def test_a_channel_with_a_v2_row_is_available_before_anything_is_tried():
    coordinator = _Coordinator(channel=11)
    coordinator.infrared_v2_row = V2_ROW

    assert not infrared_write_is_refused(coordinator)
    assert _select(coordinator).available is True


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
