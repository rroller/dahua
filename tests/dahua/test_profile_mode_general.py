"""Which day/night profile is live, across three device behaviours.

The profile selects which Lighting[channel][profile] the light is read from and
written to, so getting it wrong means the command is accepted and nothing
lights. Two camera families disagree about which field is authoritative, which
is why this has now been broken in both directions:

  #582  IL series dual smart light -- ConfigEx selects the profile and
        Config[0] stays a static 0, so reading Config[0] left the illuminator
        permanently tracking the day profile.
  #605  General profile management -- Config[0] is 2 and ConfigEx merely
        echoes day/night, so preferring ConfigEx sent every write to profile 0
        while the camera rendered from profile 2.
"""

from types import SimpleNamespace

import pytest

from custom_components.dahua import DahuaDataUpdateCoordinator


def _coordinator(channel=0):
    c = object.__new__(DahuaDataUpdateCoordinator)
    c._channel = channel
    return c


def _row(channel=0, **fields):
    return {"table.VideoInMode[{0}].{1}".format(channel, k): v for k, v in fields.items()}


# --- the two reported cameras -----------------------------------------------

def test_general_profile_management_uses_profile_two():
    """#605: Config[0]=2 wins, even though ConfigEx says Day."""
    data = _row(**{"Config[0]": "2", "ConfigEx": "Day"})

    assert _coordinator().read_profile_mode(data) == "2"


@pytest.mark.parametrize("config_ex,expected", [("Day", "0"), ("Night", "1"), ("night", "1"), (" Night ", "1")])
def test_il_series_takes_the_profile_from_config_ex(config_ex, expected):
    """#582: Config[0] is a static 0 on these, so ConfigEx is what selects."""
    data = _row(**{"Config[0]": "0", "ConfigEx": config_ex})

    assert _coordinator().read_profile_mode(data) == expected


# --- everything else --------------------------------------------------------

@pytest.mark.parametrize("config", ["0", "1"])
def test_a_camera_without_config_ex_uses_config(config):
    assert _coordinator().read_profile_mode(_row(**{"Config[0]": config})) == config


def test_nothing_reported_falls_back_to_day():
    assert _coordinator().read_profile_mode({}) == "0"


def test_an_empty_config_falls_back_to_day():
    assert _coordinator().read_profile_mode(_row(**{"Config[0]": ""})) == "0"


# --- the NVR row, which must survive all of the above ------------------------

def test_a_channel_reads_its_own_row_not_channel_ones():
    """An NVR returns a row per channel; reading row 0 gave every channel
    channel 1's profile."""
    data = {}
    data.update(_row(0, **{"Config[0]": "0"}))
    data.update(_row(3, **{"Config[0]": "1"}))

    assert _coordinator(channel=3).read_profile_mode(data) == "1"


def test_config_ex_is_also_read_from_this_channels_row():
    data = {}
    data.update(_row(0, **{"Config[0]": "0", "ConfigEx": "Day"}))
    data.update(_row(3, **{"Config[0]": "0", "ConfigEx": "Night"}))

    assert _coordinator(channel=3).read_profile_mode(data) == "1"


def test_a_channel_with_no_row_falls_back_to_row_zero():
    """A single camera reports only row 0."""
    data = _row(0, **{"Config[0]": "1"})

    assert _coordinator(channel=5).read_profile_mode(data) == "1"


def test_general_mode_is_detected_from_this_channels_row():
    data = {}
    data.update(_row(0, **{"Config[0]": "0", "ConfigEx": "Day"}))
    data.update(_row(2, **{"Config[0]": "2", "ConfigEx": "Day"}))

    assert _coordinator(channel=2).read_profile_mode(data) == "2"


# --- values we do not recognise ---------------------------------------------

@pytest.mark.parametrize("config_ex", ["Normal", "General", "Auto", "", "Daytime"])
def test_an_unrecognised_config_ex_defers_to_config(config_ex):
    """Overriding a Config[0] that is probably right, for a string we cannot
    read, is the worse guess of the two."""
    data = _row(**{"Config[0]": "1", "ConfigEx": config_ex})

    assert _coordinator().read_profile_mode(data) == "1"


# --- the shape a real NVR returns -------------------------------------------

def test_the_measured_nvr_resolves_every_channel():
    """Measured on a DHI-NVR5464-16P-EI: Config[0] is 0 everywhere except one
    channel in General mode, and only two channels report ConfigEx at all."""
    data = {}
    for channel in range(12):
        data.update(_row(channel, **{"Config[0]": "2" if channel == 9 else "0"}))
    data.update(_row(1, **{"Config[0]": "0", "ConfigEx": "Day"}))
    data.update(_row(11, **{"Config[0]": "0", "ConfigEx": "Day"}))

    assert _coordinator(channel=9).read_profile_mode(data) == "2", "the General channel"
    assert _coordinator(channel=1).read_profile_mode(data) == "0", "reports ConfigEx"
    assert _coordinator(channel=11).read_profile_mode(data) == "0"
    assert _coordinator(channel=4).read_profile_mode(data) == "0", "plain channel"
