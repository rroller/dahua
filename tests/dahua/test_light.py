"""light.py had no tests at all, and day/night handling has regressed before."""

import pytest

from custom_components.dahua import dahua_utils
from custom_components.dahua.light import DahuaIlluminator, DahuaInfraredLight

ATTR_BRIGHTNESS = "brightness"


class _Client:
    def __init__(self):
        self.v1 = []
        self.v2 = []
        self.scheme = {}
        self.scheme_calls = []
        self.scheme_reads = 0

    async def async_set_lighting_v1(self, channel, enabled, brightness, profile_mode="0"):
        self.v1.append((channel, enabled, brightness, profile_mode))

    async def async_set_lighting_v2(self, channel, enabled, brightness, profile_mode,
                                    light_index=0, bank="MiddleLight"):
        self.v2.append((channel, enabled, brightness, profile_mode, light_index, bank))

    async def async_get_lighting_scheme(self):
        """Defined so the scheme check runs for real rather than erroring out.

        Returning no scheme is the common camera: nothing blocks, nothing warns.
        """
        self.scheme_reads += 1
        return self.scheme

    async def async_set_lighting_scheme_illuminator(
            self, channel, enabled, brightness, profile_mode, light_index):
        self.scheme_calls.append(
            (channel, enabled, brightness, profile_mode, light_index)
        )


class _Coordinator:
    def __init__(self, channel=3, profile_mode="1", uses_scheme=False):
        self.client = _Client()
        self._channel = channel
        self._profile_mode = profile_mode
        self.refreshed = 0
        self.infrared_on = True
        self.infrared_brightness = 128
        self.illuminator_on = False
        self.illuminator_brightness = 64
        # Which light this device calls the white one; 0 on most models.
        self.illuminator_index = 0
        # Which brightness bank the white light uses; MiddleLight on most models.
        self.illuminator_bank = "MiddleLight"
        self.uses_scheme = uses_scheme

    def get_channel(self):
        return self._channel

    def get_profile_mode(self):
        return self._profile_mode

    def get_infrared_profile(self):
        """The profile the infrared light really uses; the live one here."""
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

    def get_illuminator_index(self):
        return self.illuminator_index

    def get_illuminator_bank(self):
        return self.illuminator_bank

    def uses_lighting_scheme_illuminator(self):
        return self.uses_scheme

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

    assert c.client.v1 == [(3, True, 100, "1")], (
        "the write must name the profile the camera is using, not 0")
    assert c.client.v2 == [], "the infrared light must not use the v2 API"
    assert c.refreshed == 1


async def test_infrared_turn_off_sends_enabled_false():
    c = _Coordinator(channel=3)
    await _light(DahuaInfraredLight, c).async_turn_off()

    assert len(c.client.v1) == 1
    channel, enabled, _, profile = c.client.v1[0]
    assert (channel, enabled) == (3, False)
    assert profile == "1", "turning off must reach the same profile as turning on"


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

    assert c.client.v2 == [(2, True, 100, "1", 0, "MiddleLight")]
    assert c.client.v1 == [], "the illuminator must not use the v1 API"


async def test_illuminator_turn_off_keeps_the_profile_mode():
    c = _Coordinator(channel=2, profile_mode="0")

    await _light(DahuaIlluminator, c, "Illuminator").async_turn_off()

    channel, enabled, _, profile_mode, _index, _bank = c.client.v2[0]
    assert (channel, enabled, profile_mode) == (2, False, "0")


async def test_a_camera_with_no_lighting_scheme_still_switches_on_cleanly():
    """The scheme check must not get in the way of the command itself."""
    c = _Coordinator(channel=2, profile_mode="0")

    await _light(DahuaIlluminator, c, "Illuminator").async_turn_on()

    assert c.client.v2, "the light command did not reach the client"
    assert c.client.scheme_reads == 1, "the scheme was not consulted"


async def test_illuminator_writes_to_the_light_the_device_calls_white():
    """On a camera that reports index 0 as infrared, writing to 0 changes a
    light nobody can see. The resolved index has to reach the client."""
    c = _Coordinator(channel=2, profile_mode="0")
    c.illuminator_index = 1

    await _light(DahuaIlluminator, c, "Illuminator").async_turn_on()

    assert c.client.v2[0][4] == 1, "the illuminator wrote to the wrong light"


async def test_illuminator_uses_whatever_profile_mode_is_current():
    c = _Coordinator(profile_mode="2")
    await _light(DahuaIlluminator, c, "Illuminator").async_turn_on()
    assert c.client.v2[0][3] == "2"


async def test_scheme_illuminator_uses_the_two_table_client_path():
    c = _Coordinator(channel=0, profile_mode="1", uses_scheme=True)
    c.illuminator_index = 1

    light = _light(DahuaIlluminator, c, "Illuminator")
    await light.async_turn_on(**{ATTR_BRIGHTNESS: 255})
    await light.async_turn_off()

    assert c.client.scheme_calls == [
        (0, True, 100, "1", 1),
        (0, False, 100, "1", 1),
    ]
    assert c.client.v2 == []


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
