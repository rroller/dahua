"""The poll must not fetch answers nothing is going to read.

Each request costs the device a connection and a login it has to refuse, so an
entry with a platform switched off should stop paying for the reads that only
that platform consumes. These pin each request to the platform that reads it.
"""

from datetime import timedelta
from types import SimpleNamespace

import pytest

from custom_components.dahua import DahuaDataUpdateCoordinator


class _Client:
    """Records which API each poll actually calls."""

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        async def call(*args, **kwargs):
            self.calls.append(name)
            return {}
        return call


def _coordinator(**options):
    c = object.__new__(DahuaDataUpdateCoordinator)
    c.client = _Client()
    c.hass = SimpleNamespace()
    c._address = "10.0.0.5"
    c._channel = 0
    c.initialized = True
    c.model = ""            # keeps every model-string capability out of the way
    c._profile_mode = 0
    c._supports_profile_mode = False
    c._supports_ptz_position = True
    c._supports_disarming_linkage = True
    c._supports_event_notifications = True
    c._supports_coaxial_control = True
    c._supports_smart_motion_detection = True
    c._supports_lighting_v2 = True
    c._supports_lighting = True          # gates supports_infrared_light()
    c._supports_floodlightmode = False
    c._channel_number = 1
    c._max_streams = 3
    c._serial_number = "SER1"
    c.machine_name = "cam"
    c._preset_position = "0"
    c.config_entry = SimpleNamespace(data={}, options=dict(options), entry_id="e1")
    c.update_interval = timedelta(seconds=30)
    return c


async def _poll(**options):
    c = _coordinator(**options)
    await c._async_update_data()
    return c.client.calls


PTZ = "async_get_ptz_position"
COAXIAL = "async_get_coaxial_control_io_status"
MOTION = "async_get_config_motion_detection"
LIGHTING_V2 = "async_get_lighting_v2"
INFRARED = "async_get_config_lighting"
DISARMING = "async_get_disarming_linkage"
NOTIFICATIONS = "async_get_event_notifications"
SMART_MOTION = "async_get_smart_motion_detection"


async def test_everything_is_fetched_when_every_platform_is_on():
    """The default must not change: this is what an untouched entry does."""
    calls = await _poll()

    for api in (PTZ, COAXIAL, MOTION, LIGHTING_V2, INFRARED, DISARMING,
                NOTIFICATIONS, SMART_MOTION):
        assert api in calls, f"{api} stopped being fetched by default"


# --- each request follows the platform that reads it -------------------------

async def test_ptz_position_is_skipped_without_the_select_platform():
    """Only the preset position select reads it, and it is uncached."""
    assert PTZ not in await _poll(select=False)


async def test_switch_reads_are_skipped_without_the_switch_platform():
    calls = await _poll(switch=False)

    for api in (DISARMING, NOTIFICATIONS, SMART_MOTION):
        assert api not in calls, f"{api} is only read by a switch"


async def test_light_reads_are_skipped_without_the_light_platform():
    calls = await _poll(light=False)

    assert LIGHTING_V2 not in calls
    assert INFRARED not in calls


# --- a request with two readers survives losing one of them ------------------

async def test_coaxial_status_survives_either_reader():
    """The siren switch and the security light both read this."""
    assert COAXIAL in await _poll(light=False), "the siren switch still needs it"
    assert COAXIAL in await _poll(switch=False), "the security light still needs it"
    assert COAXIAL not in await _poll(light=False, switch=False)


async def test_motion_detection_survives_either_reader():
    """The camera entity reads this as well as the switch."""
    assert MOTION in await _poll(switch=False), "the camera entity still needs it"
    assert MOTION in await _poll(camera=False), "the motion switch still needs it"
    assert MOTION not in await _poll(camera=False, switch=False)


# --- turning one platform off must not take another's reads with it ----------

@pytest.mark.parametrize("disabled,still_wanted", [
    ("select", [COAXIAL, MOTION, LIGHTING_V2, DISARMING]),
    ("light", [PTZ, COAXIAL, MOTION, DISARMING, SMART_MOTION]),
    ("switch", [PTZ, COAXIAL, MOTION, LIGHTING_V2, INFRARED]),
    ("binary_sensor", [PTZ, COAXIAL, MOTION, LIGHTING_V2, DISARMING]),
])
async def test_disabling_one_platform_leaves_the_others_alone(disabled, still_wanted):
    calls = await _poll(**{disabled: False})

    for api in still_wanted:
        assert api in calls, f"disabling {disabled} wrongly dropped {api}"
