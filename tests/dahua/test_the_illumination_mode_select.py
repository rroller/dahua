"""The illumination mode select: which emitter the camera may reach for.

A dual-light camera decides its own illumination, and `LightingScheme` says
which half it is allowed to use. While it reads `AIMode` or `InfraredMode` the
white light stays off whatever is written to `Lighting_V2` -- stored, reported
on, dark (#647). This control is the only way to see or change that.

Measured on a DHI-NVR5464-16P-EI driving a VSIPP-6DIRMD-I3 on channel 11. The
numbers in the capability fixtures below are that device's, including the two
shapes a channel without the hardware answers with.
"""

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.dahua import select as select_module
from custom_components.dahua.select import DahuaLightingSchemeSelect

# What the recorder really answered, per channel. Both negative shapes are
# here on purpose: five channels omit LightingMixSolution entirely and two
# carry it with Support false, so a presence check would arm those two.
CAPS_CH11 = {
    "LightingMixSolution": {
        "AIMixLightObjFilter": {"Support": True, "Type": ["Human", "Vehicle"]},
        "AIMixLightSwitchDelayRange": [30, 300],
        "LightingScheme": ["WhiteMode", "InfraredMode", "AIMode"],
        "Support": True,
        "SupportLightingSchemeSchedule": True,
    }
}
CAPS_SUPPORT_FALSE = {"LightingMixSolution": {"Support": False}}
CAPS_ABSENT: dict = {}


class _Client:
    def __init__(self, error=None):
        self.calls = []
        self._error = error

    async def async_set_channel_scoped_lighting_scheme(self, channel, mode):
        self.calls.append((channel, mode))
        if self._error is not None:
            raise self._error


class _Coordinator:
    def __init__(self, schemes=None, data=None, channel=11, error=None):
        self._schemes = schemes
        self.data = data or {}
        self._channel = channel
        self.client = _Client(error)
        self.refreshed = 0

    def supports_lighting_schemes(self):
        return bool(self._schemes)

    def get_lighting_schemes(self):
        return self._schemes

    def get_channel(self):
        return self._channel

    def get_serial_number(self):
        return "SER11"

    def get_lighting_scheme(self):
        prefix = "table.LightingScheme[{0}][".format(self._channel)
        modes = {
            value
            for key, value in (self.data or {}).items()
            if key.startswith(prefix) and key.endswith("].LightingMode")
        }
        return next(iter(modes)) if len(modes) == 1 else None

    async def async_request_refresh(self):
        self.refreshed += 1


def _select(**kwargs):
    coordinator = _Coordinator(**kwargs)
    entity = DahuaLightingSchemeSelect.__new__(DahuaLightingSchemeSelect)
    entity._coordinator = coordinator
    reported = coordinator.get_lighting_schemes() or []
    entity._modes = {
        select_module._SCHEME_OPTIONS.get(mode, mode): mode for mode in reported
    }
    entity._attr_options = list(entity._modes)
    return entity, coordinator


def _scheme_data(channel, modes):
    return {
        "table.LightingScheme[{0}][{1}].LightingMode".format(channel, index): mode
        for index, mode in enumerate(modes)
    }


# --- what the capability probe decides ---------------------------------------


@pytest.mark.parametrize(
    "caps,expected",
    [
        (CAPS_CH11, ["WhiteMode", "InfraredMode", "AIMode"]),
        (CAPS_SUPPORT_FALSE, None),
        (CAPS_ABSENT, None),
    ],
)
def test_support_is_read_from_the_flag_not_the_key(caps, expected):
    """`Support: false` must be a no, exactly like the key being missing.

    Two channels of the measured recorder carry the block and say they cannot
    do it. Testing `"LightingMixSolution" in caps` would give both of them the
    control, which is the model-whitelist bug in a new costume: believing a
    device can do something it said it could not.
    """
    solution = caps.get("LightingMixSolution")
    supported = isinstance(solution, dict) and bool(solution.get("Support"))
    schemes = solution.get("LightingScheme") if supported else None
    assert schemes == expected


# --- what the entity offers ---------------------------------------------------


def test_the_options_are_the_modes_the_camera_named():
    """Not a hard-coded three. The camera's list is what it can be put in."""
    entity, _ = _select(schemes=["WhiteMode", "InfraredMode", "AIMode"])

    assert sorted(entity._attr_options) == ["infrared", "smart_dual", "white"]


def test_a_mode_the_camera_does_not_offer_is_not_offered():
    """A camera reporting only two gets two, rather than a dropdown with a
    third entry that writes a mode it has said it does not have."""
    entity, _ = _select(schemes=["WhiteMode", "InfraredMode"])

    assert sorted(entity._attr_options) == ["infrared", "white"]
    assert "smart_dual" not in entity._attr_options


def test_an_unknown_mode_is_offered_under_its_own_name():
    """Forward compatible on purpose. A mode with no slug here is still
    selectable, because a control that hides what the device can do is worse
    than one showing an untranslated word."""
    entity, _ = _select(schemes=["AIMode", "RedBlueMode"])

    assert "RedBlueMode" in entity._attr_options


# --- what it reports ----------------------------------------------------------


def test_the_mode_is_reported_when_every_scene_agrees():
    entity, _ = _select(schemes=["AIMode"], data=_scheme_data(11, ["AIMode"] * 9))

    assert entity.current_option == "smart_dual"


def test_scenes_that_disagree_report_nothing():
    """Nine scenes, each with its own mode. Picking one would mean resolving
    which scene is live, and that resolution is most of what #959 got wrong --
    VideoInMode has three shapes on a single recorder (#458). A split set is
    genuinely not one mode, so unknown is the honest answer."""
    entity, _ = _select(
        schemes=["AIMode"],
        data=_scheme_data(11, ["AIMode"] * 8 + ["WhiteMode"]),
    )

    assert entity.current_option is None


def test_another_channels_scenes_are_not_read_as_this_ones():
    """The table is keyed by channel and a recorder serves many. Reading the
    wrong one would report a neighbour's mode with no sign anything is wrong."""
    entity, _ = _select(
        schemes=["AIMode"], channel=11, data=_scheme_data(3, ["WhiteMode"] * 9)
    )

    assert entity.current_option is None


# --- what it writes -----------------------------------------------------------


async def test_choosing_a_mode_writes_the_devices_own_spelling():
    """The slug is what Home Assistant looks a label up by; the device's word
    is what goes on the wire."""
    entity, coordinator = _select(schemes=["WhiteMode", "InfraredMode", "AIMode"])

    await entity.async_select_option("smart_dual")

    assert coordinator.client.calls == [(11, "AIMode")]
    assert coordinator.refreshed == 1, "the new mode has to be read back"


async def test_a_mode_outside_the_list_is_refused_by_key():
    """Asserted on the translation key, because a translated message only
    renders inside a running Home Assistant and the key is the half that
    decides what every language shows."""
    entity, coordinator = _select(schemes=["AIMode"])

    with pytest.raises(HomeAssistantError) as raised:
        await entity.async_select_option("white")

    assert raised.value.translation_key == "lighting_scheme_unknown"
    assert coordinator.client.calls == [], "nothing may reach the device"


async def test_a_refusal_from_the_device_reaches_the_user():
    """The client raises when the camera accepts a write and ignores it. That
    must not be swallowed into a silent no-op."""
    error = HomeAssistantError(translation_key="lighting_scheme_not_accepted")
    entity, coordinator = _select(schemes=["AIMode"], error=error)

    with pytest.raises(HomeAssistantError) as raised:
        await entity.async_select_option("smart_dual")

    assert raised.value.translation_key == "lighting_scheme_not_accepted"
    assert coordinator.refreshed == 0
