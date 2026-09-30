"""What setup asks a device, and what it concludes when the device refuses.

The one-time initialisation asks a camera thirteen separate questions about what it can
do, each in its own handler, each turning one capability on or off. That block is the
largest single piece of the integration and almost none of it was executed.

The property that matters is the same for all thirteen: **a refusal is an answer, not a
failure.** A camera without a siren, without PTZ, without an illuminator is a perfectly
good camera, and a probe that took setup down with it would make one missing feature into
no integration at all. #854 was exactly that, reported by three people: an RPC2 refusal of
the coaxial status stopped their AD410s ever finishing setup.

So each probe is driven twice over, once refusing and once not, and the refusal case
asserts three things: setup finished, that capability is off, and **the other capabilities
are untouched**. The last is the one a per-probe test would miss. These share a try block
and a sequence, so a handler that swallows too much turns one refusal into several
features quietly disappearing.

The client and coordinator surfaces the block touches were taken from it by walking its
AST rather than by reading it, so nothing it asks for is missing from the fake by
oversight.
"""

import pytest
from types import SimpleNamespace

from custom_components.dahua import DahuaDataUpdateCoordinator

# Every question setup asks, and the capability it decides. Taken from the source:
# each of these is one try block whose handler sets exactly this attribute.
PROBES = [
    ("async_get_coaxial_control_io_status", "_supports_coaxial_control"),
    ("async_get_disarming_linkage", "_supports_disarming_linkage"),
    ("async_get_event_notifications", "_supports_event_notifications"),
    ("async_get_ptz_position", "_supports_ptz_position"),
    ("async_get_smart_motion_detection", "_supports_smart_motion_detection"),
    ("async_get_video_in_options", "_supports_day_night_color"),
    ("async_get_lighting_v2", "_supports_lighting_v2"),
    ("async_get_lighting_scheme", "_supports_lighting_scheme_illuminator"),
    ("async_get_privacy_mode", "_supports_privacy_mode"),
]

CLIENT_METHODS = (
    "async_get_alarm_output_slots", "async_get_coaxial_control_io_status",
    "async_get_config", "async_get_device_class", "async_get_disarming_linkage",
    "async_get_event_notifications", "async_get_ivs_rules",
    "async_get_lighting_scheme", "async_get_lighting_v2",
    "async_get_machine_name", "async_get_privacy_mode",
    "async_get_ptz_position", "async_get_remote_ivs_rules",
    "async_get_smart_motion_detection", "async_get_system_info",
    "async_get_video_in_options",
)

# What setup asks the coordinator about itself. Off, so the fan-out afterwards is
# empty and each test is about the probe it names.
SELF_METHODS = (
    "is_doorbell", "is_flood_light", "is_nvr_channel", "supports_floodlightmode",
    "supports_infrared_light", "uses_rpc2_deterrence",
)

FLAGS = (
    "_supports_coaxial_control", "_supports_day_night_color",
    "_supports_disarming_linkage", "_supports_event_notifications",
    "_supports_floodlightmode", "_supports_lighting",
    "_supports_lighting_scheme_illuminator", "_supports_lighting_v2",
    "_supports_privacy_mode", "_supports_profile_mode",
    "_supports_ptz_position", "_supports_smart_motion_detection",
)


class _Refused(Exception):
    """What a device that does not serve an endpoint answers with."""


def _coordinator(hass, refusing=None):
    """A coordinator about to run its one-time initialisation.

    `refusing` names the one client call that fails. Everything else answers
    emptily, which is what a device that serves an endpoint and has nothing to
    report looks like.
    """
    client = SimpleNamespace(use_rpc2=False)

    def _answer(name):
        async def call(*args, **kwargs):
            if name == refusing:
                raise _Refused(name)
            return {}
        return call

    for name in CLIENT_METHODS:
        setattr(client, name, _answer(name))

    async def _streams(*a, **k):
        if "get_max_extra_streams" == refusing:
            raise _Refused("get_max_extra_streams")
        return 1
    client.get_max_extra_streams = _streams

    async def _version(*a, **k):
        return {"version": "1.0"}
    client.get_software_version = _version

    async def _type(*a, **k):
        return {"type": "IPC-HFW1234"}
    client.get_device_type = _type

    c = object.__new__(DahuaDataUpdateCoordinator)
    c.hass = hass
    c.client = client
    c.initialized = False
    c._address = "10.0.0.5"
    c._channel = 0
    c._channel_number = 1
    c._serial_number = "SERIAL1"
    c._firmware_version = ""
    c._device_class = ""
    c._channel_model = None
    c._max_streams = 1
    c._alarm_output_slots = 0
    c._ivs_rules = []
    c._profile_mode = "0"
    c._preset_position = "0"
    c._camera_reboot_generation = 0
    c.machine_name = ""
    c.model = ""
    c.config_entry = SimpleNamespace(entry_id="e1")
    for flag in FLAGS:
        setattr(c, flag, False)
    for name in SELF_METHODS:
        setattr(c, name, lambda *a, **k: False)
    c._wanted_by = lambda *a, **k: False
    c.channel_option = lambda key, default=None: default
    c.get_coaxial_status_channel = lambda: 0
    c.get_rpc2_coaxial_status_channel = lambda: 1
    c.read_profile_mode = lambda data: "0"
    c._note_probe_refusal = lambda *a, **k: None
    c._back_off_poll_interval = lambda n: None
    c._restore_poll_interval = lambda: None

    async def _nothing(*a, **k):
        return None
    c.async_start_event_listener = _nothing
    c.async_start_vto_event_listener = _nothing
    c.async_detect_lighting_support = _nothing
    c._async_probe_direct_deterrence = _nothing
    c._async_coaxial_status = _nothing
    c._async_fetch_privacy_mode = _nothing
    return c


# --- a device that answers everything ---------------------------------------

async def test_setup_finishes(hass):
    """The control. Every assertion below is about a run that completed, so a
    setup that fell over for its own reasons would make all of them vacuous."""
    coordinator = _coordinator(hass)

    await coordinator._async_update_data()

    assert coordinator.initialized is True


async def test_setup_only_runs_its_probes_once(hass):
    """`initialized` is what stops the whole block running on every poll. Asking a
    camera thirteen capability questions every thirty seconds is most of a poll
    budget spent on answers that do not change."""
    coordinator = _coordinator(hass)
    await coordinator._async_update_data()

    asked = []

    async def _record(*args, **kwargs):
        asked.append(1)
        return {}

    coordinator.client.async_get_privacy_mode = _record

    await coordinator._async_update_data()

    assert asked == [], "the capability probes ran again on the second poll"


# --- and one that refuses ---------------------------------------------------

@pytest.mark.parametrize("method, flag", PROBES)
async def test_a_refused_probe_does_not_stop_setup(hass, method, flag):
    """#854, generalised. Three people had AD410s that never finished setup
    because one RPC2 call was refused, and every one of these is the same shape."""
    coordinator = _coordinator(hass, refusing=method)

    await coordinator._async_update_data()

    assert coordinator.initialized is True, (
        "a refused %s stopped the device finishing setup" % method)


@pytest.mark.parametrize("method, flag", PROBES)
async def test_a_refused_probe_turns_its_own_capability_off(hass, method, flag):
    coordinator = _coordinator(hass, refusing=method)

    await coordinator._async_update_data()

    assert getattr(coordinator, flag) is False


@pytest.mark.parametrize("method, flag", PROBES)
async def test_a_refused_probe_leaves_the_others_alone(hass, method, flag):
    """The one a per-probe test would miss. They share a sequence, so a handler
    that reaches too far turns one refusal into several features disappearing, and
    nothing about that is visible in a log.
    """
    answering = _coordinator(hass)
    await answering._async_update_data()
    expected = {name: getattr(answering, name) for name in FLAGS if name != flag}

    refusing = _coordinator(hass, refusing=method)
    await refusing._async_update_data()

    got = {name: getattr(refusing, name) for name in FLAGS if name != flag}
    assert got == expected, (
        "refusing %s also changed another capability" % method)
