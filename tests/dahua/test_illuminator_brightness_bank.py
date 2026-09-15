"""Brightness has to go to the bank the light actually uses.

A Dahua light keeps its level in one of several named banks, and which one is
not the same for every light or every model. The integration hardcoded
MiddleLight, which is right for the infrared emitter on most cameras and often
wrong for the white one.

That was harmless while the illuminator also wrote to the infrared row. Since
#652 it writes to whichever row the device calls white, so a hardcoded
MiddleLight now aims brightness at a bank that row may not have:

    IPC-HFW2449T-AS-IL   [0] InfraredLight -> MiddleLight
                         [1] WhiteLight    -> NearLight
    DHI-NVR5464-16P-EI   [x][0] InfraredLight -> MiddleLight
                         [x][1] WhiteLight    -> no bank at all

Falling back to MiddleLight when the device names none is what every caller did
before this existed, so a silent device keeps the behaviour it had.
"""

from custom_components.dahua import illuminator_brightness_bank


def _light(channel, profile, index, bank):
    return {"table.Lighting_V2[{0}][{1}][{2}].{3}[0].Light".format(
        channel, profile, index, bank): "50"}


# --- the models this exists for -----------------------------------------------

def test_a_white_light_on_nearlight_is_found():
    """The IPC-HFW2449T-AS-IL layout: infrared on Middle, white on Near."""
    data = _light(3, 0, 0, "MiddleLight")
    data.update(_light(3, 0, 1, "NearLight"))
    assert illuminator_brightness_bank(data, 3, 0, 1) == "NearLight"


def test_the_infrared_row_still_resolves_to_middlelight():
    data = _light(3, 0, 0, "MiddleLight")
    data.update(_light(3, 0, 1, "NearLight"))
    assert illuminator_brightness_bank(data, 3, 0, 0) == "MiddleLight"


def test_farlight_is_found_when_it_is_the_only_one():
    assert illuminator_brightness_bank(_light(0, 0, 1, "FarLight"), 0, 0, 1) == "FarLight"


# --- and what must not change -------------------------------------------------

def test_a_light_naming_no_bank_falls_back_to_middlelight():
    """A WhiteLight row with no bank at all, as one NVR reports."""
    assert illuminator_brightness_bank({}, 0, 0, 1) == "MiddleLight"


def test_middlelight_wins_when_a_light_exposes_several():
    """Preserve the bank this integration has always written to."""
    data = _light(0, 0, 1, "MiddleLight")
    data.update(_light(0, 0, 1, "NearLight"))
    assert illuminator_brightness_bank(data, 0, 0, 1) == "MiddleLight"


def test_another_lights_bank_is_not_borrowed():
    """Index 0's bank must not answer for index 1."""
    assert illuminator_brightness_bank(_light(0, 0, 0, "NearLight"), 0, 0, 1) == "MiddleLight"


def test_another_channel_or_profile_is_not_borrowed():
    data = _light(0, 0, 1, "NearLight")
    assert illuminator_brightness_bank(data, 5, 0, 1) == "MiddleLight"
    assert illuminator_brightness_bank(data, 0, 1, 1) == "MiddleLight"
