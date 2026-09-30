"""What the light entities read off the last poll.

Three readers on the coordinator, none of them executed by the suite. They are short and
they look obvious, which is exactly how both of the bugs recorded in their own comments
got in: the brightness reader had the profile hardcoded to 0 while `is_illuminator_on`
read the live one, so a camera running Night reported its Day brightness, and the
brightness bank was hardcoded the same way.

Neither of those raises. The slider simply shows a number from the wrong profile, and the
only way to notice is to know what the camera is actually set to.

So every test here is written against a `data` map that has **different values in the day
and night profiles, and in the near and middle banks**. A reader that goes to the wrong
one gets a different number rather than the same one, which is the only way these can
tell a fixed index from a resolved one.
"""

import pytest

from custom_components.dahua import DahuaDataUpdateCoordinator

CHANNEL = 2
DAY = "0"
NIGHT = "1"


def _coordinator(data=None, *, profile=NIGHT, index=0, bank="MiddleLight",
                 scheme=False, floodlightmode=False):
    c = object.__new__(DahuaDataUpdateCoordinator)
    c._channel = CHANNEL
    c.data = dict(data or {})
    c._supports_floodlightmode = floodlightmode
    c.get_profile_mode = lambda: profile
    c.get_illuminator_index = lambda: index
    c.get_illuminator_bank = lambda: bank
    c.uses_lighting_scheme_illuminator = lambda: scheme
    return c


def _key(field, profile=NIGHT, index=0, channel=CHANNEL):
    return "table.Lighting_V2[%s][%s][%s].%s" % (channel, profile, index, field)


# --- which brightness field this camera uses --------------------------------

def test_a_camera_with_a_near_light_uses_it():
    coordinator = _coordinator({_key("NearLight[0].Light"): "50"})

    assert coordinator.get_illuminator_brightness_field() == "NearLight"


def test_a_camera_with_a_middle_light_uses_that():
    coordinator = _coordinator({_key("MiddleLight[0].Light"): "50"})

    assert coordinator.get_illuminator_brightness_field() == "MiddleLight"


def test_a_camera_with_both_prefers_the_near_light():
    """Deliberate and worth pinning, because the order of the two checks is the
    whole of the decision."""
    coordinator = _coordinator({
        _key("NearLight[0].Light"): "50",
        _key("MiddleLight[0].Light"): "80",
    })

    assert coordinator.get_illuminator_brightness_field() == "NearLight"


def test_a_camera_reporting_neither_falls_back_to_the_middle_light():
    """A poll that has not read the table yet, or a device that does not serve it.
    MiddleLight is the common case, and guessing it is better than returning
    nothing to a caller that formats it into a URL."""
    assert _coordinator({}).get_illuminator_brightness_field() == "MiddleLight"


def test_the_field_is_looked_up_in_the_live_profile():
    """The bug this file exists for. With the profile fixed at 0, a camera running
    Night would look in the Day row, find nothing, and fall back to MiddleLight
    while its NearLight sat in the Night row."""
    coordinator = _coordinator(
        {_key("NearLight[0].Light", profile=NIGHT): "50"}, profile=NIGHT)

    assert coordinator.get_illuminator_brightness_field() == "NearLight"

    day_only = _coordinator(
        {_key("NearLight[0].Light", profile=DAY): "50"}, profile=NIGHT)

    assert day_only.get_illuminator_brightness_field() == "MiddleLight", (
        "it read the day profile while the camera is on night")


def test_the_field_is_looked_up_at_the_resolved_light_index():
    """Index 0 on most models and 1 on some. Looking in the wrong one finds
    nothing and silently falls back."""
    coordinator = _coordinator(
        {_key("NearLight[0].Light", index=1): "50"}, index=1)

    assert coordinator.get_illuminator_brightness_field() == "NearLight"


# --- and what it reads out of it --------------------------------------------

def test_the_brightness_comes_from_the_live_profile_and_bank():
    """Two different numbers in the two profiles, so a reader that went to the
    wrong one gets a different answer rather than the same one by luck."""
    coordinator = _coordinator({
        _key("MiddleLight[0].Light", profile=DAY): "0",
        _key("MiddleLight[0].Light", profile=NIGHT): "100",
    }, profile=NIGHT)

    assert coordinator.get_illuminator_brightness() == 255


def test_reading_the_day_profile_while_on_night_is_a_different_number():
    """The negative control for the line above, written out because the original
    bug was exactly this and it reported a plausible number rather than failing."""
    coordinator = _coordinator({
        _key("MiddleLight[0].Light", profile=DAY): "0",
        _key("MiddleLight[0].Light", profile=NIGHT): "100",
    }, profile=DAY)

    assert coordinator.get_illuminator_brightness() == 0


def test_the_brightness_comes_from_the_resolved_bank():
    """The other half of the same bug. The bank was hardcoded, so a camera whose
    white light is on NearLight reported the MiddleLight number."""
    coordinator = _coordinator({
        _key("NearLight[0].Light"): "100",
        _key("MiddleLight[0].Light"): "0",
    }, bank="NearLight")

    assert coordinator.get_illuminator_brightness() == 255


def test_a_scheme_camera_reads_a_percentage_instead():
    """Cameras driven through LightingScheme report a percentage in their own
    field rather than a bank. Reading the bank on one of those finds nothing."""
    coordinator = _coordinator(
        {_key("PercentOfMaxBrightness"): "50"}, scheme=True)

    assert coordinator.get_illuminator_brightness() == 127


def test_a_missing_brightness_reads_as_full():
    """`None` converts to 255. Worth stating, because it means a camera that has
    not answered yet shows a full slider rather than an empty one, and that is a
    deliberate choice rather than an accident of the conversion."""
    assert _coordinator({}).get_illuminator_brightness() == 255


# --- whether the flood light is on ------------------------------------------

@pytest.mark.parametrize("reported, expected", [
    ("On", True), ("on", True), ("ON", True), ("Off", False), ("", False),
])
def test_a_floodlightmode_camera_reads_its_coaxial_status(reported, expected):
    """These report through the coaxial status rather than the lighting table.
    The comparison is lowercased because firmware disagrees about the casing."""
    coordinator = _coordinator({"status.WhiteLight": reported},
                               floodlightmode=True)

    assert coordinator.is_flood_light_on() is expected


def test_an_amcrest_flood_light_reads_the_lighting_table():
    """The other kind. Light index 1 here is not the resolved illuminator index:
    the flood light is its own emitter."""
    coordinator = _coordinator(
        {_key("Mode", index=1): "Manual"}, floodlightmode=False)

    assert coordinator.is_flood_light_on() is True


def test_an_amcrest_flood_light_that_is_off():
    coordinator = _coordinator(
        {_key("Mode", index=1): "Off"}, floodlightmode=False)

    assert coordinator.is_flood_light_on() is False


def test_an_amcrest_flood_light_reads_the_live_profile():
    """Manual in the day row while the camera is on night is not the light being
    on, and the entity would otherwise report it as on for as long as the
    profile lasted."""
    coordinator = _coordinator(
        {_key("Mode", profile=DAY, index=1): "Manual"}, profile=NIGHT)

    assert coordinator.is_flood_light_on() is False
