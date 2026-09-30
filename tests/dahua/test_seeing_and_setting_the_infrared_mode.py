"""`Auto` was indistinguishable from `Off`, and the level was invisible.

`is_infrared_light_on()` is `Mode == "Manual"`, and the light entity writes only
`Manual` or `Off`. So on a camera left at `Auto` -- which is what they ship on --
the emitter illuminates every night and the entity reads **off**. Measured on a
DHI-NVR5464-16P-EI:

    ch 0,1,2,3,5,6,7,8,9,10,12,13   Auto        <- 12 channels, all read off
    ch 4                            Manual      <- the only one reading on
    ch 11                           ZoomPrio    <- a mode nothing here writes
    ch 14                           Off

Thirteen of those fifteen channels can never read on, and nothing in Home
Assistant could tell `Auto` from `Off` or hand a channel back to the camera's own
judgement once something had written `Manual`.

The level had the same shape of problem from the other side: Home Assistant
publishes a light's `brightness` only while it is on, so a channel on `Auto` at
`MiddleLight[0].Light=50` reported `brightness: None`. The level the camera is
actually using was not readable anywhere.
"""

import pytest

from custom_components.dahua.infrared import MODE_BY_OPTION, OPTION_BY_MODE
from custom_components.dahua.light import DahuaInfraredLight
from custom_components.dahua.select import DahuaInfraredModeSelect

from .test_light import _Coordinator as _LightCoordinator, _light


class _Coordinator:
    """Just what the mode select reads."""

    subentry_id = None

    def __init__(self, mode="Auto", level=50, channel=3):
        self.mode = mode
        self.level = level
        self._channel = channel
        self.refreshed = 0
        self.written = []
        self.client = self

    def get_channel(self):
        return self._channel

    def get_serial_number(self):
        return "SERIAL1"

    def get_device_name(self):
        return "Driveway"

    def get_address(self):
        """DahuaBaseEntity.available reads it, and the select now builds on that."""
        return "192.168.0.213"

    def get_infrared_profile(self):
        return "0"

    def get_infrared_mode(self):
        return self.mode

    def get_infrared_level(self):
        return self.level

    async def async_set_lighting_v1_mode(self, channel, mode, brightness,
                                         profile_mode="0"):
        self.written.append((channel, mode, brightness, profile_mode))
        self.mode = mode

    async def async_refresh(self):
        self.refreshed += 1


def _mode_select(coordinator):
    entity = object.__new__(DahuaInfraredModeSelect)
    entity._coordinator = coordinator
    entity.coordinator = coordinator
    return entity


# --- what the dropdown shows ---------------------------------------------------

@pytest.mark.parametrize("mode,option", [
    ("Auto", "auto"),
    ("Manual", "manual"),
    ("Off", "off"),
])
def test_the_mode_the_device_reports_is_the_option_shown(mode, option):
    assert _mode_select(_Coordinator(mode=mode)).current_option == option


@pytest.mark.parametrize("mode", ["ZoomPrio", "SmartLight", "", None])
def test_a_mode_this_integration_does_not_write_shows_as_unknown(mode):
    """Home Assistant rejects a `current_option` outside `options`, so a mode the
    device chose for itself has to come back None rather than be forced into one
    of the three. Two of this recorder's channels report `ZoomPrio`."""
    assert _mode_select(_Coordinator(mode=mode)).current_option is None


def test_the_options_are_slugs_because_that_is_what_the_lookup_needs():
    """A label is looked up at `entity.select.infrared_mode.state.<option>`, and
    that lookup only works for a slug -- so the options cannot be the device's own
    capitalised words. Asserted here because the failure is silent: the dropdown
    simply shows the raw option in every language."""
    entity = _mode_select(_Coordinator())

    assert entity.options == ["auto", "manual", "off"]
    for option in entity.options:
        assert option == option.lower()
        assert MODE_BY_OPTION[option] in ("Auto", "Manual", "Off")


def test_the_two_maps_are_each_other():
    """A one-way map would make a mode readable and unsettable, or the reverse."""
    assert OPTION_BY_MODE == {mode: option for option, mode in MODE_BY_OPTION.items()}
    assert set(MODE_BY_OPTION.values()) == set(OPTION_BY_MODE)


# --- what selecting one writes -------------------------------------------------

@pytest.mark.parametrize("option,mode", [
    ("auto", "Auto"),
    ("manual", "Manual"),
    ("off", "Off"),
])
async def test_selecting_a_mode_writes_the_device_spelling(option, mode):
    coordinator = _Coordinator(mode="Manual", level=60)
    await _mode_select(coordinator).async_select_option(option)

    assert coordinator.written == [(3, mode, 60, "0")]
    assert coordinator.refreshed == 1


async def test_auto_is_reachable_which_is_the_point_of_this_entity():
    """The light entity writes only Manual or Off. Handing a channel back to the
    camera's own judgement was not possible from Home Assistant at all."""
    coordinator = _Coordinator(mode="Manual")
    await _mode_select(coordinator).async_select_option("auto")

    assert [mode for _c, mode, _b, _p in coordinator.written] == ["Auto"]


async def test_selecting_a_mode_keeps_the_level_the_camera_is_using():
    """The write carries a brightness whether or not one was asked for, so taking
    the default would quietly dim or brighten the emitter on every mode change."""
    coordinator = _Coordinator(mode="Auto", level=35)
    await _mode_select(coordinator).async_select_option("manual")

    assert coordinator.written[0][2] == 35


async def test_a_channel_reporting_no_level_is_written_at_full():
    coordinator = _Coordinator(mode="Auto", level=None)
    await _mode_select(coordinator).async_select_option("manual")

    assert coordinator.written[0][2] == 100


async def test_an_option_that_is_not_offered_writes_nothing():
    """Nothing in the frontend sends one, but a service call can."""
    coordinator = _Coordinator(mode="Auto")
    await _mode_select(coordinator).async_select_option("ZoomPrio")

    assert coordinator.written == []
    assert coordinator.refreshed == 0


# --- and the light says what it could not say before ---------------------------

def test_the_light_reports_the_mode_and_level_even_when_it_reads_off():
    """The Auto case, which is twelve of this recorder's fifteen channels."""
    coordinator = _LightCoordinator()
    coordinator.client.infrared_mode = "Auto"
    coordinator.infrared_on = False
    coordinator.infrared_level = 50
    light = _light(DahuaInfraredLight, coordinator)

    assert light.is_on is False
    assert light.extra_state_attributes == {"mode": "Auto", "brightness_level": 50}


def test_a_mode_the_device_chose_is_named_on_the_light_even_though_the_select_cannot():
    """The select shows unknown for ZoomPrio because HA rejects an option outside
    the list. The attribute is where the real answer stays readable."""
    coordinator = _LightCoordinator()
    coordinator.client.infrared_mode = "ZoomPrio"
    light = _light(DahuaInfraredLight, coordinator)

    assert light.extra_state_attributes["mode"] == "ZoomPrio"


def test_a_channel_with_no_lighting_row_reports_nothing_rather_than_zero():
    """An absent level must not read as an emitter at zero, which is a different
    and wrong claim."""
    coordinator = _LightCoordinator()
    coordinator.client.infrared_mode = ""
    coordinator.infrared_level = None
    light = _light(DahuaInfraredLight, coordinator)

    assert light.extra_state_attributes == {"mode": None, "brightness_level": None}
