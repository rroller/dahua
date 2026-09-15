"""Off is not the same as automatic.

The light entity can only say on or off, which writes `Manual` or `Off` to the
illuminator. `Off` leaves the camera's own illumination disabled until someone
puts it back, and until now nothing in Home Assistant could: infrared has had a
mode service with Auto since the beginning, the illuminator never did (#540).

These pin the URL the service builds, because every part of it has been wrong at
some point this month -- the light index (#652), the brightness bank (#659) and
the profile.
"""

from unittest.mock import AsyncMock

from custom_components.dahua.client import DahuaClient


def _client():
    c = object.__new__(DahuaClient)
    c.get = AsyncMock(return_value={})
    return c


async def _call(mode, brightness=100, profile="1", index=1, bank="NearLight"):
    c = _client()
    await DahuaClient.async_set_lighting_v2_mode(
        c, 0, mode, brightness, profile, index, bank)
    return c.get.await_args.args[0]


# --- the gap this closes ------------------------------------------------------

async def test_auto_hands_control_back_to_the_camera():
    url = await _call("Auto")
    assert "Mode=Auto" in url


async def test_on_is_written_as_manual():
    """The infrared service accepts On and means Manual; match it."""
    assert "Mode=Manual" in await _call("On")


async def test_lowercase_is_accepted():
    """vol.In allows 'auto', so the client must not depend on the capital."""
    assert "Mode=Auto" in await _call("auto")


async def test_off_is_still_off():
    assert "Mode=Off" in await _call("Off")


# --- and it must address the same light the toggle does -----------------------

async def test_it_writes_the_resolved_light_index():
    """Index 0 is the infrared emitter on dual-light cameras; see #652."""
    url = await _call("Auto", index=1)
    assert "Lighting_V2[0][1][1].Mode=" in url


async def test_it_writes_the_resolved_brightness_bank():
    """The white light is not always on MiddleLight; see #659."""
    url = await _call("Auto", brightness=40, bank="NearLight")
    assert "NearLight[0].Light=40" in url
    assert "MiddleLight" not in url


async def test_it_writes_the_live_profile():
    url = await _call("Auto", profile="2")
    assert "Lighting_V2[0][2][" in url
