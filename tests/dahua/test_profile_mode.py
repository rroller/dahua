"""VideoInMode is one host-wide read with a row per channel."""

import pytest

from custom_components.dahua import DahuaDataUpdateCoordinator

# What an eleven channel NVR returns. Channel 0 is on the day profile, most of
# the rest are on night, and channel 4 has been put on the scene profile.
NVR_TABLE = {
    "table.VideoInMode[0].Config[0]": "0",
    "table.VideoInMode[0].Mode": "0",
    "table.VideoInMode[1].Config[0]": "1",
    "table.VideoInMode[2].Config[0]": "1",
    "table.VideoInMode[3].Config[0]": "1",
    "table.VideoInMode[4].Config[0]": "2",
    "table.VideoInMode[5].Config[0]": "1",
    "table.VideoInMode[6].Config[0]": "1",
    "table.VideoInMode[7].Config[0]": "1",
    "table.VideoInMode[8].Config[0]": "1",
    "table.VideoInMode[9].Config[0]": "1",
    "table.VideoInMode[10].Config[0]": "0",
}

# A single camera returns one row, and that row is row 0.
SINGLE_CAMERA_TABLE = {
    "table.VideoInMode[0].Config[0]": "1",
    "table.VideoInMode[0].Mode": "0",
}


def _coordinator(channel):
    coordinator = object.__new__(DahuaDataUpdateCoordinator)
    coordinator._channel = channel
    return coordinator


@pytest.mark.parametrize("channel,expected", [
    (0, "0"),
    (1, "1"),
    (4, "2"),
    (10, "0"),
])
def test_each_channel_reads_its_own_row(channel, expected):
    """Reading row 0 for every channel gave the whole NVR channel 1's profile."""
    assert _coordinator(channel).read_profile_mode(NVR_TABLE) == expected


def test_the_profile_is_not_the_same_for_every_channel():
    """The bug stated as a property: eleven channels are not one answer."""
    modes = {_coordinator(c).read_profile_mode(NVR_TABLE) for c in range(11)}

    assert len(modes) > 1


# --- a single camera must be untouched --------------------------------------

def test_a_single_camera_still_reads_row_zero():
    assert _coordinator(0).read_profile_mode(SINGLE_CAMERA_TABLE) == "1"


def test_a_channel_with_no_row_of_its_own_does_not_borrow_row_zero():
    """This assertion used to read the other way round.

    Row 0 belongs to whichever camera reported it. A channel that has no row of
    its own must not adopt another camera's day/night profile -- the profile
    decides which Lighting_V2[channel][profile] every light command for this
    camera is written to, so borrowing one sends those writes to a profile the
    camera is not using, where the device accepts and ignores them.

    Falling through to the documented default instead makes the answer depend
    on this channel alone.
    """
    assert _coordinator(3).read_profile_mode(SINGLE_CAMERA_TABLE) == "0"


def test_a_recorder_does_not_give_every_channel_camera_ones_profile():
    """The field case. Camera 1 is on day, channel 6 reports no row at all."""
    table = {"table.VideoInMode[0].Config[0]": "1",
             "table.VideoInMode[1].Config[0]": "1"}

    assert _coordinator(0).read_profile_mode(table) == "1"
    assert _coordinator(1).read_profile_mode(table) == "1"
    assert _coordinator(6).read_profile_mode(table) == "0", (
        "channel 6 has no row and took camera 1's night profile")


def test_one_answer_is_never_assembled_from_two_cameras():
    """The fallback was per field, so Config[0] and ConfigEx could disagree.

    Channel 5 has a Config[0] of its own and no ConfigEx. Row 0's ConfigEx said
    night, and taking it turned this channel's day into night.
    """
    table = {"table.VideoInMode[0].ConfigEx": "night",
             "table.VideoInMode[0].Config[0]": "1",
             "table.VideoInMode[5].Config[0]": "0"}

    assert _coordinator(5).read_profile_mode(table) == "0"


# --- the old defaults ------------------------------------------------------

def test_an_empty_table_is_the_day_profile():
    assert _coordinator(0).read_profile_mode({}) == "0"
    assert _coordinator(7).read_profile_mode({}) == "0"


def test_a_blank_value_is_the_day_profile():
    """The device answering with nothing must not become an empty profile,
    which would then be interpolated into Lighting[channel][]."""
    assert _coordinator(0).read_profile_mode(
        {"table.VideoInMode[0].Config[0]": ""}
    ) == "0"


def test_a_blank_value_on_this_channel_does_not_fall_through_to_row_zero():
    """A row that exists and is blank is this channel's answer, not row 0's."""
    table = dict(NVR_TABLE)
    table["table.VideoInMode[4].Config[0]"] = ""

    assert _coordinator(4).read_profile_mode(table) == "0"


def test_the_profile_is_always_a_string():
    """It is interpolated straight into a config name, so it cannot be None."""
    for table in (NVR_TABLE, SINGLE_CAMERA_TABLE, {}):
        for channel in (0, 4, 10):
            assert isinstance(_coordinator(channel).read_profile_mode(table), str)
