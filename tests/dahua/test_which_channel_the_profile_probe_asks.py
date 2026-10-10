"""The profile-mode probe has to ask about the channel it is for.

`_supports_profile_mode` decides whether the poll reads `VideoInMode` at all,
and `VideoInMode` is what sets `_profile_mode` -- the index every lighting read
and every lighting write for the channel is addressed with. The probe asked
`Lighting[0][2]`, hardcoded, so on a merged recorder (#827, one entry per
device with a subentry per channel) every channel's answer came from camera 1's
table.

The failure is the one `read_profile_mode` and `infrared_profile` each have a
paragraph about, arrived at from a different direction. With the flag wrongly
False, the poll never reads `VideoInMode`, `_profile_mode` stays `"0"`, and a
camera running Night has its lighting read from and written to the Day row --
where the device accepts the write, answers OK, and goes on rendering from the
profile it is actually using. Nothing in the log, and the light reports a state
it is not in.

It is lopsided, which is why it survived: the reverse case (channel 0 has
profiles, channel N does not) lands on the same-channel fallback both of those
functions already have, so it is harmless. Only a recorder whose channel 0 has
fewer profiles than its other channels is affected, and a single camera -- which
is channel 0 -- can never see it at all.

The harness is `test_what_setup_probes.py`'s, trimmed. Its client surface was
taken from the initialisation block by walking its AST rather than by reading
it, so nothing the block asks for is missing from the fake by oversight; the
only thing added here is a record of the config names asked.

The mutation this is written against, which I cannot execute here (the suite
needs Home Assistant and this module imports it): putting `Lighting[0][2]` back
makes `test_a_channel_does_not_inherit_channel_zeros_profiles` fail, which is
the bug.
"""

from types import SimpleNamespace

from custom_components.dahua import DahuaDataUpdateCoordinator
from custom_components.dahua.client import DahuaClient

# What a camera with selectable profiles answers for its own row: more than one
# line. One line is the device's "Error: Error -1 getting param in name=..."
# reply, which is what the probe reads as no support.
HAS_PROFILES = {
    "table.Lighting[{0}][2].Mode": "Auto",
    "table.Lighting[{0}][2].MiddleLight[0].Light": "50",
}


class _Client:
    """Answers everything emptily, and records the config names it was asked."""

    use_rpc2 = False
    device_key = "10.0.0.5:80"

    SHAPED = {
        "get_max_extra_streams": 1,
        "get_software_version": {"version": "1.0"},
        "get_device_type": {"type": "IPC-HFW1234"},
    }

    def __init__(self, profiles_on=()):
        # Which channels report a profile 2 row of their own.
        self._profiles_on = set(profiles_on)
        self.lighting_asked = []

    async def async_get_config_lighting(self, channel, profile_mode):
        """The real method's shape: it builds Lighting[channel][profile]."""
        self.lighting_asked.append((int(channel), int(profile_mode)))
        if int(channel) in self._profiles_on:
            return {key.format(channel): value for key, value in HAS_PROFILES.items()}
        # One line, which is what the device sends for a row it does not have.
        return {"Error": "Error -1 getting param in name=Lighting"}

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        assert hasattr(DahuaClient, name), (
            "setup asked the client for %s, which DahuaClient does not have" % name
        )

        async def call(*args, **kwargs):
            return self.SHAPED.get(name, {})

        return call


FLAGS = (
    "_supports_cloud_upgrade",
    "_supports_coaxial_control",
    "_supports_day_night_color",
    "_supports_disarming_linkage",
    "_supports_event_notifications",
    "_supports_floodlightmode",
    "_supports_lighting",
    "_supports_lighting_scheme_illuminator",
    "_supports_lighting_v2",
    "_supports_privacy_mode",
    "_supports_profile_mode",
    "_supports_ptz_position",
    "_supports_smart_motion_detection",
)

SELF_METHODS = (
    "is_doorbell",
    "is_flood_light",
    "is_nvr_channel",
    "supports_floodlightmode",
    "supports_infrared_light",
    "uses_rpc2_deterrence",
)


def _coordinator(hass, channel, profiles_on=()):
    """A coordinator for `channel`, about to run its one-time initialisation."""
    client = _Client(profiles_on)

    c = object.__new__(DahuaDataUpdateCoordinator)
    c.hass = hass
    c.client = client
    c.initialized = False
    c._address = "10.0.0.5"
    c._channel = channel
    c._channel_number = channel + 1
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
    c._wanted_by = lambda *a, **k: True
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


async def test_the_probe_asks_about_its_own_channel(hass):
    """The fix, stated as the request that goes out."""
    coordinator = _coordinator(hass, channel=3, profiles_on=(3,))

    await coordinator._async_update_data()

    assert (3, 2) in coordinator.client.lighting_asked, (
        "the probe did not ask about channel 3: %s" % coordinator.client.lighting_asked
    )


async def test_a_channel_with_profiles_of_its_own_supports_them(hass):
    coordinator = _coordinator(hass, channel=3, profiles_on=(3,))

    await coordinator._async_update_data()

    assert coordinator.supports_profile_mode() is True


async def test_a_channel_does_not_inherit_channel_zeros_profiles(hass):
    """The bug. Channel 0 has profiles and channel 3 does not, and the hardcoded
    name asked about channel 0 -- so channel 3 reported support it does not have,
    the poll read VideoInMode for it, and the profile that came back was camera
    1's."""
    coordinator = _coordinator(hass, channel=3, profiles_on=(0,))

    await coordinator._async_update_data()

    assert coordinator.supports_profile_mode() is False


async def test_a_channel_with_profiles_is_not_denied_them_by_channel_zero(hass):
    """The same bug the other way round, which is the half that loses a feature
    rather than misreporting one: with channel 0 flat and channel 3 carrying four
    profiles, channel 3 was told it had none, so its lighting was addressed as
    Day while the camera rendered from Night."""
    coordinator = _coordinator(hass, channel=3, profiles_on=(3,))

    await coordinator._async_update_data()

    assert coordinator.supports_profile_mode() is True
    assert (0, 2) not in coordinator.client.lighting_asked, (
        "it still asked about channel 0: %s" % coordinator.client.lighting_asked
    )


async def test_a_single_camera_is_unaffected(hass):
    """A standalone camera is channel 0, so the old name and the new one are the
    same request. Nothing about the common case changes."""
    coordinator = _coordinator(hass, channel=0, profiles_on=(0,))

    await coordinator._async_update_data()

    assert coordinator.supports_profile_mode() is True
    assert coordinator.client.lighting_asked == [(0, 2)]


async def test_a_channel_reporting_one_line_has_no_profiles(hass):
    """The device's own "Error -1 getting param" reply is one line, and that is
    what the probe reads as no support. Judged on what came back rather than on
    an exception, because async_get_config swallows a ClientResponseError and
    returns {}."""
    coordinator = _coordinator(hass, channel=0, profiles_on=())

    await coordinator._async_update_data()

    assert coordinator.supports_profile_mode() is False
