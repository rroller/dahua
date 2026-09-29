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
    # `model = ""` above keeps the model-string capabilities out of the way, which also
    # means this device has no siren and no security light. The poll now asks whether any
    # entity will read the coaxial status before fetching it, so the reads these tests pin
    # need a reader to exist. Overriding the leaf capabilities rather than
    # creates_siren_entity keeps the real rule in play.
    c.supports_siren = lambda: True
    c.supports_security_light = lambda: True
    # is_nvr_channel reads this, and the poll asks it when choosing the
    # coaxial channel. object.__new__ means an attribute the class sets in
    # __init__ does not exist here unless it is named.
    c._nvr_active_deterrence = False
    c._supports_smart_motion_detection = True
    c._alarm_output_slots = 1
    c._supports_lighting_v2 = True
    c._supports_privacy_mode = True
    c._supports_day_night_color = True
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
PRIVACY = "async_get_privacy_mode"
DAY_NIGHT = "async_get_video_in_options"
ALARM_OUT = "async_get_alarm_output_state"
CLOUD_UPGRADE = "async_get_cloud_upgrade_info"


async def test_everything_is_fetched_when_every_platform_is_on():
    """The default must not change: this is what an untouched entry does."""
    calls = await _poll()

    for api in (PTZ, COAXIAL, MOTION, LIGHTING_V2, INFRARED, DISARMING,
                NOTIFICATIONS, SMART_MOTION, PRIVACY, DAY_NIGHT):
        assert api in calls, f"{api} stopped being fetched by default"


# --- each request follows the platform that reads it -------------------------

async def test_ptz_position_is_skipped_without_the_select_platform():
    """Only the preset position select reads it, and it is uncached."""
    assert PTZ not in await _poll(select=False)


async def test_the_day_night_read_is_skipped_without_the_select_platform():
    """VideoInOptions is 1998 lines on a recorder, so it is worth not asking.

    Only the Day/Night select reads it.
    """
    assert DAY_NIGHT not in await _poll(select=False)


async def test_the_day_night_read_is_skipped_when_the_device_has_no_mode():
    """A device that reported no DayNightColor must not be polled for one."""
    c = _coordinator()
    c._supports_day_night_color = False

    await c._async_update_data()

    assert DAY_NIGHT not in c.client.calls


async def test_switch_reads_are_skipped_without_the_switch_platform():
    calls = await _poll(switch=False)

    for api in (DISARMING, NOTIFICATIONS, SMART_MOTION, PRIVACY, ALARM_OUT):
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


async def test_lighting_v2_survives_losing_the_light_platform_on_a_doorbell():
    """The Amcrest doorbell's "Security Light" is a *select*, not a light.

    Its current_option reads table.Lighting_V2[0][0][1].Mode and .State, so
    gating that table on the light platform alone left the entity reading an
    absent table and reporting "Off" forever.
    """
    c = _coordinator(light=False)
    c.model = "AD410"

    await c._async_update_data()

    assert LIGHTING_V2 in c.client.calls, "the Security Light select still reads it"


async def test_the_doorbell_keeps_it_only_while_something_reads_it():
    c = _coordinator(light=False, select=False)
    c.model = "AD410"

    await c._async_update_data()

    assert LIGHTING_V2 not in c.client.calls


async def test_a_doorbell_with_no_security_light_does_not_start_fetching_it():
    """The gate mirrors the condition select.py creates that entity under, so
    nothing that never had the select begins paying for the read."""
    c = _coordinator(light=False)
    c.model = "DB600"       # an Amcrest doorbell, but no security light
    # The shared fake grants this so the coaxial reads above have a reader. Here the
    # model's own answer is the point, so it goes back.
    c.supports_security_light = lambda: False

    await c._async_update_data()

    assert LIGHTING_V2 not in c.client.calls


# --- turning one platform off must not take another's reads with it ----------

@pytest.mark.parametrize("disabled,still_wanted", [
    ("select", [COAXIAL, MOTION, LIGHTING_V2, DISARMING, PRIVACY]),
    ("light", [PTZ, COAXIAL, MOTION, DISARMING, SMART_MOTION, PRIVACY]),
    ("switch", [PTZ, COAXIAL, MOTION, LIGHTING_V2, INFRARED]),
    ("binary_sensor", [PTZ, COAXIAL, MOTION, LIGHTING_V2, DISARMING, PRIVACY]),
])
async def test_disabling_one_platform_leaves_the_others_alone(disabled, still_wanted):
    calls = await _poll(**{disabled: False})

    for api in still_wanted:
        assert api in calls, f"disabling {disabled} wrongly dropped {api}"


# --- and it is not fetched for a device with nothing that reads it ------------

async def test_a_device_with_nothing_that_reads_the_coaxial_status_is_not_asked():
    """`_supports_coaxial_control` means the endpoint answers, not that this device has a
    siren or a light.

    Measured on a DHI-NVR5464: eleven entries, no siren and no security light entity
    anywhere, and the endpoint asked on every poll. At a 120 second interval that is on
    the order of 7,900 requests a day for a value nothing displays.
    """
    c = _coordinator()
    c.supports_siren = lambda: False
    c.supports_security_light = lambda: False

    await c._async_update_data()

    assert COAXIAL not in c.client.calls


async def test_a_flood_light_still_gets_the_coaxial_status():
    """The reader that is easy to forget. `is_flood_light_on` reads WhiteLight out of this
    same status when the camera reports floodlightmode, so a flood light camera has to keep
    being asked even with no siren and no security light."""
    c = _coordinator()
    c.supports_siren = lambda: False
    c.supports_security_light = lambda: False
    c.is_flood_light = lambda: True
    c._supports_floodlightmode = True

    await c._async_update_data()

    assert COAXIAL in c.client.calls


async def test_a_flood_light_without_floodlightmode_is_not_asked():
    """On that firmware the same entity reads Lighting_V2 instead."""
    c = _coordinator()
    c.supports_siren = lambda: False
    c.supports_security_light = lambda: False
    c.is_flood_light = lambda: True
    c._supports_floodlightmode = False

    await c._async_update_data()

    assert COAXIAL not in c.client.calls


# --- the cloud upgrade record belongs to the update platform -----------------

async def test_the_cloud_upgrade_record_is_not_read_without_the_update_platform():
    """It feeds only the informational update entity."""
    c = _coordinator(update=False)
    c._supports_cloud_upgrade = True

    await c._async_update_data()

    assert CLOUD_UPGRADE not in c.client.calls


async def test_the_cloud_upgrade_record_is_read_once_and_reused():
    """The device rewrites it only after its own OTA check, so re-reading it
    every poll would be thousands of requests for a value that has not moved."""
    c = _coordinator()
    c._supports_cloud_upgrade = True

    await c._async_update_data()
    await c._async_update_data()

    assert c.client.calls.count(CLOUD_UPGRADE) == 1
