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

from custom_components.dahua import DahuaDataUpdateCoordinator, infrared_profile


def _coordinator(channel, profile_mode, data):
    c = object.__new__(DahuaDataUpdateCoordinator)
    c._channel = channel
    c._profile_mode = profile_mode
    c.data = data
    return c


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
    assert c.get_infrared_brightness() == 255, "the documented default when nothing is reported"
