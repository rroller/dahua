"""Setting Day/Night on a device that has no VideoInDayNight table.

#687 reports a DHI-VTO2311R-WP where `dahua.set_video_in_day_night_mode` always
returns 400. The reporter traced what the VTO's own web UI writes and found it
uses a different table entirely:

    VideoInOptions[0].DayNightColor    0 = Color, 1 = Auto, 2 = BlackWhite

Measured here afterwards, read-only:

    DHI-NVR5464-16P-EI   VideoInDayNight -> 400 Bad Request
                         VideoInOptions  -> 1998 lines, DayNightColor per channel
    VTO                  VideoInDayNight -> "Unknown error"
                         VideoInOptions  -> 32 lines, VideoInOptions[0].DayNightColor

So neither of my devices carries VideoInDayNight either, and all three carry
VideoInOptions. The existing write is tried first and kept for whatever device it
was written for; the fallback runs only when that one is refused.

The fallback key is deliberately the bare one. VideoInOptions also carries
NightOptions.DayNightColor and NormalOptions.DayNightColor, and the bare one is
what the web UI exposes and what #687 verified. So `config_type` does not apply
on that path.
"""

import aiohttp
import pytest

from custom_components.dahua.client import DahuaClient, DAY_NIGHT_COLOR


class _Client(DahuaClient):
    """The real method, with get() replaced by a recorder."""

    def __init__(self, day_night_reply):
        self.urls = []
        self._day_night_reply = day_night_reply

    async def get(self, url, verify_ok=False):
        self.urls.append(url)
        if "VideoInDayNight" in url:
            if isinstance(self._day_night_reply, Exception):
                raise self._day_night_reply
            return self._day_night_reply
        return "OK"


def _refused():
    return aiohttp.ClientResponseError(None, None, status=400, message="Bad Request")


# --- the device that has the old table --------------------------------------

async def test_a_device_with_videoindaynight_is_unchanged():
    c = _Client("OK")

    await c.async_set_video_in_day_night_mode(0, "general", "Color")

    assert len(c.urls) == 1, "the fallback must not run when the first write worked"
    assert "VideoInDayNight[0][2].Mode=Color" in c.urls[0]


# --- the devices that do not -------------------------------------------------

async def test_a_refused_write_falls_back_to_videoinoptions():
    c = _Client(_refused())

    await c.async_set_video_in_day_night_mode(0, "general", "Color")

    assert len(c.urls) == 2
    assert "VideoInOptions[0].DayNightColor=0" in c.urls[1]


async def test_a_reply_that_is_not_ok_also_falls_back():
    """A device can answer 200 with something other than OK."""
    c = _Client("")

    await c.async_set_video_in_day_night_mode(0, "general", "BlackWhite")

    assert len(c.urls) == 2
    assert "VideoInOptions[0].DayNightColor=2" in c.urls[1]


@pytest.mark.parametrize("mode,expected", [
    ("Color", 0), ("color", 0),
    ("Auto", 1), ("auto", 1), ("Brightness", 1), (None, 1),
    ("BlackWhite", 2), ("blackwhite", 2),
])
async def test_every_mode_maps_to_the_documented_integer(mode, expected):
    c = _Client(_refused())

    await c.async_set_video_in_day_night_mode(0, "general", mode)

    assert c.urls[1].endswith("DayNightColor=%d" % expected)


async def test_the_channel_is_named_on_the_fallback_path():
    c = _Client(_refused())

    await c.async_set_video_in_day_night_mode(5, "general", "Color")

    assert "VideoInOptions[5].DayNightColor=" in c.urls[1]


async def test_the_fallback_uses_the_bare_key_not_a_profile_scoped_one():
    """NightOptions and NormalOptions variants exist; the web UI writes neither."""
    c = _Client(_refused())

    await c.async_set_video_in_day_night_mode(0, "night", "Color")

    assert "NightOptions" not in c.urls[1]
    assert "NormalOptions" not in c.urls[1]


async def test_a_fallback_that_also_fails_still_raises():
    """Silence would leave the user believing the mode changed."""

    class _AllRefused(_Client):
        async def get(self, url, verify_ok=False):
            self.urls.append(url)
            return ""

    c = _AllRefused(_refused())

    with pytest.raises(Exception):
        await c.async_set_video_in_day_night_mode(0, "general", "Color")


def test_the_mapping_is_the_one_the_device_documented():
    assert DAY_NIGHT_COLOR == {"Color": 0, "Brightness": 1, "BlackWhite": 2}
