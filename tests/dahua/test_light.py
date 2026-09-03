"""light.py had no tests at all, and day/night handling has regressed before."""

import pytest

from custom_components.dahua import dahua_utils
from custom_components.dahua.light import DahuaIlluminator, DahuaInfraredLight

ATTR_BRIGHTNESS = "brightness"


class _Client:
    def __init__(self):
        self.v1 = []
        self.v2 = []

    async def async_set_lighting_v1(self, channel, enabled, brightness):
        self.v1.append((channel, enabled, brightness))

    async def async_set_lighting_v2(self, channel, enabled, brightness, profile_mode):
        self.v2.append((channel, enabled, brightness, profile_mode))


class _Coordinator:
    def __init__(self, channel=3, profile_mode="1"):
        self.client = _Client()
        self._channel = channel
        self._profile_mode = profile_mode
        self.refreshed = 0
        self.infrared_on = True
        self.infrared_brightness = 128
        self.illuminator_on = False
        self.illuminator_brightness = 64

    def get_channel(self):
        return self._channel

    def get_profile_mode(self):
        return self._profile_mode

    def get_serial_number(self):
        return "SERIAL1"

    def get_device_name(self):
        return "Front Door"

    def is_infrared_light_on(self):
        return self.infrared_on

    def get_infrared_brightness(self):
        return self.infrared_brightness

    def is_illuminator_on(self):
        return self.illuminator_on

    def get_illuminator_brightness(self):
        return self.illuminator_brightness

    async def async_refresh(self):
        self.refreshed += 1


def _light(cls, coordinator, name="Infrared"):
    """Build the entity without Home Assistant's entity plumbing."""
    entity = object.__new__(cls)
    entity._coordinator = coordinator
    entity.coordinator = coordinator
    entity._name = name
    return entity


# --- brightness conversion -------------------------------------------------

@pytest.mark.parametrize("hass_value,expected", [
    (0, 0),
    (255, 100),
    (128, 50),
    (None, 100),   # no brightness given means full, not off
])
def test_hass_brightness_maps_to_dahua_scale(hass_value, expected):
    assert dahua_utils.hass_brightness_to_dahua_brightness(hass_value) == expected


@pytest.mark.parametrize("dahua_value,expected", [
    ("0", 0),
    ("100", 255),
    ("50", 127),
    ("", 255),     # blank means full
    (None, 255),
])
def test_dahua_brightness_maps_back_to_hass_scale(dahua_value, expected):
    assert dahua_utils.dahua_brightness_to_hass_brightness(dahua_value) == expected


def test_the_two_conversions_agree_on_what_no_value_means():
    """Both default to "full", but they work on different scales.

    Full is 100 on the Dahua scale and 255 on the HASS scale. Using 100 for
    both made an unspecified brightness mean 39%, so a plain toggle-on lit
    the light dimly.
    """
    no_value_hass = dahua_utils.dahua_brightness_to_hass_brightness(None)
    no_value_dahua = dahua_utils.hass_brightness_to_dahua_brightness(None)

    assert no_value_hass == 255, "full on the HASS scale"
    assert no_value_dahua == 100, "full on the Dahua scale"
    assert dahua_utils.dahua_brightness_to_hass_brightness(str(no_value_dahua)) == no_value_hass


def test_full_and_off_survive_a_round_trip():
    for hass_value in (0, 255):
        dahua = dahua_utils.hass_brightness_to_dahua_brightness(hass_value)
        assert dahua_utils.dahua_brightness_to_hass_brightness(str(dahua)) == hass_value


# --- infrared light (v1 API) ----------------------------------------------

async def test_infrared_turn_on_sends_the_channel_and_brightness():
    c = _Coordinator(channel=3)
    await _light(DahuaInfraredLight, c).async_turn_on(**{ATTR_BRIGHTNESS: 255})

    assert c.client.v1 == [(3, True, 100)]
    assert c.client.v2 == [], "the infrared light must not use the v2 API"
    assert c.refreshed == 1


async def test_infrared_turn_off_sends_enabled_false():
    c = _Coordinator(channel=3)
    await _light(DahuaInfraredLight, c).async_turn_off()

    assert len(c.client.v1) == 1
    channel, enabled, _ = c.client.v1[0]
    assert (channel, enabled) == (3, False)


async def test_infrared_turn_on_without_brightness_uses_full():
    c = _Coordinator()
    await _light(DahuaInfraredLight, c).async_turn_on()

    assert c.client.v1[0][2] == 100


def test_infrared_reads_state_from_the_coordinator():
    c = _Coordinator()
    light = _light(DahuaInfraredLight, c)

    assert light.is_on is True
    assert light.brightness == 128
    c.infrared_on = False
    assert light.is_on is False


# --- illuminator (v2 API, carries the profile mode) ------------------------

async def test_illuminator_passes_the_profile_mode_through():
    """Day/night handling rides on this argument and has regressed before."""
    c = _Coordinator(channel=2, profile_mode="1")

    await _light(DahuaIlluminator, c, "Illuminator").async_turn_on(**{ATTR_BRIGHTNESS: 255})

    assert c.client.v2 == [(2, True, 100, "1")]
    assert c.client.v1 == [], "the illuminator must not use the v1 API"


async def test_illuminator_turn_off_keeps_the_profile_mode():
    c = _Coordinator(channel=2, profile_mode="0")

    await _light(DahuaIlluminator, c, "Illuminator").async_turn_off()

    channel, enabled, _, profile_mode = c.client.v2[0]
    assert (channel, enabled, profile_mode) == (2, False, "0")


async def test_illuminator_uses_whatever_profile_mode_is_current():
    c = _Coordinator(profile_mode="2")
    await _light(DahuaIlluminator, c, "Illuminator").async_turn_on()
    assert c.client.v2[0][3] == "2"


# --- identity --------------------------------------------------------------

def test_the_two_lights_do_not_share_a_unique_id():
    """A collision would merge two different lights into one entity."""
    c = _Coordinator()
    infrared = _light(DahuaInfraredLight, c).unique_id
    illuminator = _light(DahuaIlluminator, c, "Illuminator").unique_id

    assert infrared == "SERIAL1_infrared"
    assert illuminator == "SERIAL1_illuminator"
    assert infrared != illuminator


def test_name_is_prefixed_with_the_device_name():
    c = _Coordinator()
    assert _light(DahuaInfraredLight, c, "Infrared").name == "Front Door Infrared"
