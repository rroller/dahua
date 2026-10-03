"""A doorbell's infrared has no brightness bank, so the write must not send one.

#963: a VTO2202F threw on
    setConfig&Lighting[0][0].Mode=Auto&Lighting[0][0].MiddleLight[0].Light=100

Measured on a VTO2000A doorbell: it serves Lighting, but Lighting[0][0] is `Mode`
only -- no MiddleLight, NearLight or FarLight field. A Mode-only write is accepted
(200 OK, value-preserving). The brightness term the integration appended targeted a
field the row does not have, and the device threw.

infrared_brightness_bank now returns None when the row names no bank, and
async_set_lighting_v1_mode writes the mode alone when the bank is None. A camera with
a real bank is unchanged.
"""

from unittest.mock import AsyncMock

from custom_components.dahua import DahuaDataUpdateCoordinator
from custom_components.dahua.coordinator import infrared_brightness_bank
from custom_components.dahua.client import DahuaClient


# --- the bank detector --------------------------------------------------------

def test_a_mode_only_row_has_no_bank():
    """The doorbell shape: Mode and nothing else."""
    data = {"table.Lighting[0][0].Mode": "Off"}

    assert infrared_brightness_bank(data, 0, "0") is None


def test_an_empty_table_has_no_bank():
    assert infrared_brightness_bank({}, 0, "0") is None


def test_a_row_with_middlelight_still_reports_it():
    """A camera is unchanged: a present bank is still found and named."""
    data = {"table.Lighting[0][0].Mode": "Manual",
            "table.Lighting[0][0].MiddleLight[0].Light": "100"}

    assert infrared_brightness_bank(data, 0, "0") == "MiddleLight"


def test_near_and_far_are_still_found():
    data = {"table.Lighting[3][0].NearLight[0].Light": "50",
            "table.Lighting[3][0].FarLight[0].Light": "50"}

    assert infrared_brightness_bank(data, 3, "0") == "NearLight"


# --- the write ----------------------------------------------------------------

def _client():
    client = object.__new__(DahuaClient)
    client.get = AsyncMock(return_value={})
    return client


async def _url(bank, mode="Manual", brightness=100, channel=0, profile="0"):
    client = _client()
    await DahuaClient.async_set_lighting_v1_mode(
        client, channel, mode, brightness, profile, bank)
    return client.get.await_args.args[0]


async def test_no_bank_writes_mode_alone():
    """The #963 fix: no brightness term when the device has no bank for it."""
    url = await _url(None)

    assert "Lighting[0][0].Mode=Manual" in url
    assert "Light=" not in url
    assert "MiddleLight" not in url


async def test_a_bank_still_writes_brightness():
    """A camera with a real bank keeps the brightness term exactly as before."""
    url = await _url("MiddleLight", brightness=100)

    assert "Lighting[0][0].Mode=Manual" in url
    assert "Lighting[0][0].MiddleLight[0].Light=100" in url


async def test_the_default_is_unchanged_for_callers_that_pass_no_bank():
    """The signature default stays MiddleLight so the existing callers and tests
    that pass no bank are untouched. Only the coordinator passes None, and it does
    so explicitly once it has found the row has no bank. So this fix is opt-in at
    the one call site that knows, not a behaviour change for everyone."""
    client = _client()
    await DahuaClient.async_set_lighting_v1_mode(client, 0, "Manual", 100)

    assert "MiddleLight[0].Light=100" in client.get.await_args.args[0]


async def test_off_on_a_doorbell_is_just_mode():
    """What rroller's doorbell actually needs: Mode=Off, nothing else."""
    url = await _url(None, mode="off", brightness=100)

    assert "Lighting[0][0].Mode=Off" in url
    assert "Light=" not in url


# --- the level read -----------------------------------------------------------

def _coordinator(data, channel=0, profile="0"):
    c = object.__new__(DahuaDataUpdateCoordinator)
    c._channel = channel
    c._profile_mode = profile
    c.data = data
    return c


def test_level_is_none_when_there_is_no_bank():
    """A doorbell has no level to report, and None says 'not known' rather than
    reading a Lighting[c][p].None[0].Light key that cannot exist."""
    c = _coordinator({"table.Lighting[0][0].Mode": "Off"})

    assert c.get_infrared_level() is None


def test_level_is_read_when_a_bank_is_present():
    c = _coordinator({"table.Lighting[0][0].Mode": "Manual",
                      "table.Lighting[0][0].MiddleLight[0].Light": "80"})

    assert c.get_infrared_level() == 80
