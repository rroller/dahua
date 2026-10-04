"""The infrared light must read and write the profile the camera is using.

The v1 Lighting table is indexed [channel][profile], exactly as Lighting_V2 is,
and the profiles genuinely differ. Measured on a DHI-NVR5464-16P-EI, where five
of fifteen channels report four profiles each and their modes disagree:

    table.Lighting[3][0].Mode=Auto
    table.Lighting[3][1].Mode=ZoomPrio
    table.Lighting[3][2].Mode=ZoomPrio
    table.Lighting[3][3].Mode=ZoomPrio

The poll has always fetched the *live* profile --
async_get_config_lighting(channel, self._profile_mode) -- while the reader and
the writer both hardcoded profile 0. So on any camera not running day:

  - the data holds one profile and the entity read another, so is_infrared_light_on
    looked up a key that was not there and reported off whatever the light was doing
  - get_infrared_brightness found nothing and fell back to its 100 default, i.e. 255
  - every write went to a profile the camera is not rendering from, where the
    device accepts it and nothing happens

This is the #605 fault in the one lighting path #659 and #683 never touched.
"""

import pytest

from unittest.mock import AsyncMock

from custom_components.dahua import DahuaDataUpdateCoordinator, infrared_profile
from custom_components.dahua.client import DahuaClient


def _coordinator(channel, profile_mode, data):
    c = object.__new__(DahuaDataUpdateCoordinator)
    c._channel = channel
    c._profile_mode = profile_mode
    c.data = data
    return c


def test_a_v1_only_channel_reads_its_mode_without_needing_the_address():
    """Reading the mode must not depend on the refusal store, and through it on the
    device address.

    `infrared_uses_lighting_v2` asks the refusal store whether v1 has been refused,
    which needs `get_address()`, which reads `_address`. This file builds the real
    coordinator with `object.__new__` and never sets it -- as does any caller holding
    a half-constructed one -- and five tests here broke the first time that read was
    added. A channel with no v2 row has no fallback, so the answer is v1 whatever the
    store says, and the question should not be asked at all.

    `get_address` is made to raise rather than left unset, so this fails loudly if the
    short-circuit is ever removed instead of depending on an attribute's absence.
    """
    coordinator = _coordinator(3, "0", dict(NVR))

    def _explode():
        raise AssertionError(
            "the refusal store was consulted for a channel with no v2 row"
        )

    coordinator.get_address = _explode

    assert coordinator.infrared_uses_lighting_v2() is False
    assert coordinator.get_infrared_mode() == "Auto"
    assert coordinator.get_infrared_level() == 50


# The measured channel 3, whose profiles disagree.
NVR = {
    "table.Lighting[3][0].Mode": "Auto",
    "table.Lighting[3][0].MiddleLight[0].Light": "50",
    "table.Lighting[3][1].Mode": "Manual",
    "table.Lighting[3][1].MiddleLight[0].Light": "100",
    "table.Lighting[3][2].Mode": "ZoomPrio",
    "table.Lighting[3][3].Mode": "ZoomPrio",
}

# What most channels of that recorder report: one profile, and it is 0.
SINGLE = {
    "table.Lighting[5][0].Mode": "Manual",
    "table.Lighting[5][0].MiddleLight[0].Light": "70",
}


# --- picking the profile ------------------------------------------------------


def test_the_live_profile_is_used_when_the_device_reports_it():
    assert infrared_profile(NVR, 3, "1") == "1"
    assert infrared_profile(NVR, 3, "2") == "2"


def test_a_device_reporting_only_profile_zero_still_uses_it():
    assert infrared_profile(SINGLE, 5, "0") == "0"


def test_a_profile_the_lighting_table_does_not_have_falls_back_to_zero():
    """VideoInMode can name a profile Lighting does not carry."""
    assert infrared_profile(SINGLE, 5, "1") == "0"


def test_the_fallback_never_leaves_this_channel():
    """Unlike the row 0 fallbacks removed in #679 and #683, this one is safe:
    it can only ever return this camera's own row."""
    both = dict(NVR)
    both.update(SINGLE)

    assert infrared_profile(both, 5, "2") == "0", "channel 5 has no profile 2"
    assert infrared_profile(both, 3, "2") == "2"


def test_the_profile_is_always_a_string():
    """It is interpolated straight into a config name."""
    assert infrared_profile(NVR, 3, 1) == "1"
    assert isinstance(infrared_profile({}, 0, 0), str)


def test_nothing_reported_is_profile_zero():
    assert infrared_profile({}, 0, "1") == "0"


# --- reading the state --------------------------------------------------------


def test_the_light_is_read_from_the_live_profile():
    """Profile 1 is Manual; profile 0 is Auto. The camera is on 1."""
    assert _coordinator(3, "1", NVR).is_infrared_light_on() is True


def test_the_day_profile_is_not_read_when_the_camera_is_on_night():
    """Reading profile 0 here would report off while the light is on."""
    assert _coordinator(3, "0", NVR).is_infrared_light_on() is False


def test_the_brightness_comes_from_the_live_profile():
    assert _coordinator(3, "1", NVR).get_infrared_brightness() == 255
    assert _coordinator(3, "0", NVR).get_infrared_brightness() == 127


def test_a_single_profile_camera_is_unchanged():
    c = _coordinator(5, "0", SINGLE)

    assert c.is_infrared_light_on() is True
    assert c.get_infrared_brightness() == 178


def test_a_missing_row_does_not_raise():
    c = _coordinator(9, "1", {})

    assert c.is_infrared_light_on() is False
    assert (
        c.get_infrared_brightness() == 255
    ), "the documented default when nothing is reported"


# --- and the write, which is the half the docstring above is actually about ---
#
# Everything above is the read side: which profile the coordinator resolves, and reading
# the mode and brightness out of it. The URL the write builds had no test at all, and
# "every write went to a profile the camera is not rendering from, where the device
# accepts it and nothing happens" is the write side of the same fault.
#
# That failure mode is the quiet kind: the request succeeds, the camera acknowledges it,
# and the light does not change. Nothing surfaces to say why.


def _client():
    client = object.__new__(DahuaClient)
    client.get = AsyncMock(return_value={})
    return client


async def _url(mode="Manual", brightness=50, channel=0, profile="0"):
    client = _client()
    await DahuaClient.async_set_lighting_v1_mode(
        client, channel, mode, brightness, profile
    )
    return client.get.await_args.args[0]


async def test_the_write_goes_to_the_profile_it_was_given():
    """The fault this file is about, from the writing end. A camera running night is
    rendering from profile 1, and a write to 0 is accepted and does nothing."""
    assert "Lighting[0][1].Mode=" in await _url(profile="1")


async def test_a_camera_on_profile_zero_still_writes_to_zero():
    """The common case has to keep working: most cameras run General and report one
    profile."""
    assert "Lighting[0][0].Mode=" in await _url(profile="0")


async def test_the_brightness_goes_to_the_same_profile_as_the_mode():
    """Both halves of the write are indexed, and splitting them across profiles would
    set the mode on one and the brightness on another."""
    url = await _url(profile="2", brightness=70)

    assert "Lighting[0][2].Mode=" in url
    assert "Lighting[0][2].MiddleLight[0].Light=70" in url


async def test_the_channel_is_the_one_asked_for():
    assert "Lighting[3][1].Mode=" in await _url(channel=3, profile="1")


# --- the mode the device will accept -----------------------------------------


async def test_on_is_written_as_manual():
    """The service takes On because that is what a person says; the API wants Manual."""
    assert "Mode=Manual" in await _url(mode="On")


async def test_lowercase_on_is_manual_too():
    assert "Mode=Manual" in await _url(mode="on")


async def test_auto_hands_control_back_to_the_camera():
    assert "Mode=Auto" in await _url(mode="auto")


async def test_off_stays_off():
    assert "Mode=Off" in await _url(mode="off")


async def test_the_first_character_is_capitalised_for_the_device():
    """Dahua wants the capital, and the service schema accepts either, so the client is
    where the two are reconciled."""
    assert "Mode=Manual" in await _url(mode="manual")


# --- the on/off wrapper the light entity uses -------------------------------


async def test_turning_the_light_on_writes_manual_to_the_live_profile():
    client = _client()

    await DahuaClient.async_set_lighting_v1(client, 0, True, 40, "1")

    url = client.get.await_args.args[0]
    assert "Lighting[0][1].Mode=Manual" in url
    assert "Lighting[0][1].MiddleLight[0].Light=40" in url


async def test_turning_the_light_off_writes_off_not_auto():
    """Off is not automatic. It leaves the camera's own illumination disabled, which is
    the documented behaviour of the switch and the reason the mode service exists."""
    client = _client()

    await DahuaClient.async_set_lighting_v1(client, 0, False, 40, "1")

    assert "Lighting[0][1].Mode=Off" in client.get.await_args.args[0]


async def test_the_wrapper_does_not_lose_the_profile():
    """It has a default of "0", so a caller that forgets to pass one silently writes to
    day. The light entity passes the live profile, and this is what says the wrapper
    carries it through rather than dropping it."""
    client = _client()

    await DahuaClient.async_set_lighting_v1(client, 2, True, 100, "3")

    assert "Lighting[2][3].Mode=Manual" in client.get.await_args.args[0]
