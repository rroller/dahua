"""Reading the Day/Night mode back, not only setting it.

#687 wanted this: a DHI-VTO2311R-WP reverts from Color to Auto after a power cut
and then renders black and white at night. The service to set the mode has
existed for some time, but nothing read it, so there was no way to notice the
device had changed by itself.

The value lives at VideoInOptions[channel].DayNightColor, which is what the
device's own web UI writes (#687 verified it by hand) and what #691 falls back
to. Measured here read-only:

    DHI-NVR5464-16P-EI   1998 lines, DayNightColor per channel
    VTO                  32 lines, VideoInOptions[0].DayNightColor=1

Neither narrower spelling works -- `name=VideoInOptions[0]` and
`name=VideoInOptions[0].DayNightColor` both return an empty 200 on both devices
-- so the table is read whole. It is a host-wide getConfig, so the shared read
cache holds it for 300s and answers it once for every channel of a recorder.
"""

import pytest

from custom_components.dahua import (
    DahuaDataUpdateCoordinator,
    DAY_NIGHT_NAMES,
    day_night_color_name,
)

# What the VTO on #687 reports, and what my own VTO reports.
VTO = {"table.VideoInOptions[0].DayNightColor": "1"}

# One channel of a recorder, with its neighbours differing.
NVR = {
    "table.VideoInOptions[0].DayNightColor": "0",
    "table.VideoInOptions[3].DayNightColor": "2",
    "table.VideoInOptions[3].NightOptions.DayNightColor": "1",
    "table.VideoInOptions[3].NormalOptions.DayNightColor": "1",
}


def _coordinator(channel, data):
    c = object.__new__(DahuaDataUpdateCoordinator)
    c._channel = channel
    c.data = data
    return c


# --- reading the value --------------------------------------------------------

def test_the_documented_values_map_to_the_service_names():
    assert DAY_NIGHT_NAMES == {"0": "Color", "1": "Auto", "2": "BlackWhite"}


def test_a_vto_reports_its_mode():
    assert day_night_color_name(VTO, 0) == "Auto"


def test_each_channel_reads_its_own_row():
    assert day_night_color_name(NVR, 0) == "Color"
    assert day_night_color_name(NVR, 3) == "BlackWhite"


def test_the_profile_scoped_rows_are_not_read():
    """VideoInOptions also carries NightOptions and NormalOptions variants.

    Channel 3's bare value is BlackWhite while both of its profile-scoped rows
    say Auto, so reading the wrong one is visible rather than theoretical.
    """
    assert day_night_color_name(NVR, 3) == "BlackWhite"


# --- what must not be guessed at ----------------------------------------------

def test_a_device_that_reports_nothing_has_no_mode():
    """None, not Color. A device without this setting must not be shown as
    though it were in one."""
    assert day_night_color_name({}, 0) is None
    assert day_night_color_name(NVR, 7) is None


def test_a_value_outside_the_documented_range_is_not_invented():
    assert day_night_color_name({"table.VideoInOptions[0].DayNightColor": "9"}, 0) is None
    assert day_night_color_name({"table.VideoInOptions[0].DayNightColor": ""}, 0) is None


def test_whitespace_is_tolerated():
    assert day_night_color_name({"table.VideoInOptions[0].DayNightColor": " 2 "}, 0) == "BlackWhite"


# --- the coordinator's half ---------------------------------------------------

def test_the_coordinator_reads_this_channel():
    assert _coordinator(3, NVR).get_day_night_color() == "BlackWhite"
    assert _coordinator(0, NVR).get_day_night_color() == "Color"


def test_the_coordinator_reports_nothing_when_the_device_did():
    assert _coordinator(9, NVR).get_day_night_color() is None
