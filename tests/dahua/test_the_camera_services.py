"""What each camera service actually sends, and which channel it sends it to.

Every service the camera platform registers is a thin method on `DahuaCamera`: read a
channel, call one client method, usually refresh. Twenty-six of them, and none was
executed by the suite, which accounted for most of `camera.py`'s uncovered lines.

Thin does not mean safe, because of the channel. The entity holds two:

    _logical_channel   0-based index, what the CGI config tables are keyed by
    _channel_number    1-based number, what the media and PTZ paths use

Most services take the first. `async_ptz_move` and `async_goto_preset_position` take the
second. Nothing enforces that, the two are equal for nobody, and they differ by one for
everybody, so passing the wrong one moves a different camera on a recorder and silently
targets channel 0 or 2 instead of 1 on a single camera. It is exactly the shape of the
offset bugs this integration keeps finding, and it cannot be seen by reading either
method on its own. So every test below names the channel it expects.

The fake client records calls through `__getattr__`, which would otherwise let a
production call to a method the real client does not have pass quietly. It does not:
every recorded name is checked against `DahuaClient` as it is recorded. An invented
method name has reached CI here before.
"""

import pytest

from homeassistant.exceptions import HomeAssistantError

from custom_components.dahua.camera import DahuaCamera, PTZ_MOVE_CODES
from custom_components.dahua.client import DahuaClient

# Deliberately different from each other and from zero, so a test cannot pass on
# either being a default.
LOGICAL = 2
NUMBER = 3


class _Client:
    """Records what was called. Every name is checked against the real client."""

    def __init__(self, raises=None):
        self.calls = []
        self._raises = raises or {}

    def __getattr__(self, name):
        if name.startswith("_"):
            # Dunder lookups from the test machinery are not client calls, and
            # answering them with a recorder would make this object pretend to
            # be iterable, awaitable and whatever else was asked for.
            raise AttributeError(name)
        assert hasattr(DahuaClient, name), (
            "camera.py called client.%s, which DahuaClient does not have" % name)

        async def record(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            if name in self._raises:
                raise self._raises[name]

        return record

    def only(self):
        """The single call, or a readable failure naming what really happened."""
        assert len(self.calls) == 1, self.calls
        return self.calls[0]


class _Coordinator:
    def __init__(self, model="IPC-HDW1234", vto_client=None,
                 profile_is_writable=True):
        self.client = _Client()
        self.refreshed = 0
        self.model = model
        self.profile_is_writable = profile_is_writable
        self.vto_client = vto_client

    def get_model(self):
        return self.model

    def video_profile_mode_is_writable(self):
        """Whether writing Config[0] selects the profile on this channel.

        Overridden per test where the point is the warning; True here so the
        existing assertions stay about which call the service makes.
        """
        return self.profile_is_writable

    def describe_video_profile_shape(self):
        return "ordinary" if self.profile_is_writable else "general"

    def get_infrared_profile(self):
        return "0"

    def get_infrared_bank(self):
        """The service passes it through, the same as the light entity does."""
        return "MiddleLight"

    def get_infrared_v2_row(self):
        """None: this file's camera is a single camera on the v1 path."""
        return None

    def get_profile_mode(self):
        return "1"

    def get_illuminator_index(self):
        return 1

    def get_illuminator_bank(self):
        return "NearLight"

    def get_vto_client(self):
        return self.vto_client

    def get_device_name(self):
        return "Front Door"

    def is_motion_detection_enabled(self):
        return True

    async def async_refresh(self):
        self.refreshed += 1


def _camera(coordinator=None, **kwargs):
    """Only what the service methods read. `Camera.__init__` is not run: it wants
    Home Assistant's entity plumbing, and none of these methods touch it."""
    coordinator = coordinator or _Coordinator(**kwargs)
    camera = object.__new__(DahuaCamera)
    camera._coordinator = coordinator
    camera._logical_channel = LOGICAL
    camera._channel_number = NUMBER
    camera._name = "Main"
    return camera


# --- motion detection, which the camera platform offers as a standard service ---

async def test_enabling_motion_detection_uses_the_logical_channel():
    camera = _camera()

    await camera.async_enable_motion_detection()

    assert camera._coordinator.client.only() == (
        "enable_motion_detection", (LOGICAL, True), {})
    assert camera._coordinator.refreshed == 1


async def test_disabling_motion_detection_sends_False():
    """The only difference between the two methods, and they are separate methods
    rather than one with an argument, so the pair has to be checked together."""
    camera = _camera()

    await camera.async_disable_motion_detection()

    assert camera._coordinator.client.only() == (
        "enable_motion_detection", (LOGICAL, False), {})
    assert camera._coordinator.refreshed == 1


async def test_a_device_that_cannot_do_motion_detection_does_not_raise():
    """A TypeError here means the device answered in a shape the client could not
    use. Home Assistant calls this from the standard camera service, so raising
    would surface as a failed service call on a camera that simply lacks the
    feature."""
    coordinator = _Coordinator()
    coordinator.client = _Client(raises={"enable_motion_detection": TypeError()})
    camera = _camera(coordinator)

    await camera.async_enable_motion_detection()

    assert coordinator.refreshed == 0, "the refresh should not have been reached"


async def test_the_same_guard_is_on_the_disable_path():
    """Written out twice in the source, so tested twice. The first one having a
    guard says nothing about the second."""
    coordinator = _Coordinator()
    coordinator.client = _Client(raises={"enable_motion_detection": TypeError()})

    await _camera(coordinator).async_disable_motion_detection()


# --- the two lights ---------------------------------------------------------

async def test_the_infrared_service_sends_the_infrared_profile():
    """`get_infrared_profile` rather than `get_profile_mode`: the infrared light
    does not always live in the same profile as everything else."""
    camera = _camera()

    await camera.async_set_infrared_mode("Manual", 50)

    assert camera._coordinator.client.only() == (
        "async_set_lighting_v1_mode", (LOGICAL, "Manual", 50, "0", "MiddleLight"), {})
    assert camera._coordinator.refreshed == 1


async def test_the_illuminator_service_addresses_the_same_light_as_the_toggle():
    """Index and bank come from the coordinator, not from constants. A camera
    whose white light is index 1 on NearLight would otherwise have the service and
    the light entity driving two different lights."""
    camera = _camera()

    await camera.async_set_illuminator_mode("Manual", 80)

    assert camera._coordinator.client.only() == (
        "async_set_lighting_v2_mode",
        (LOGICAL, "Manual", 80, "1", 1, "NearLight"), {})
    assert camera._coordinator.refreshed == 1


# --- the two that use the other channel -------------------------------------

async def test_ptz_move_uses_the_channel_number_not_the_index():
    """The PTZ path is 1-based. Sending the logical index would pan the camera one
    place along on a recorder, and on a single camera it would address channel 0,
    which some firmware accepts and acts on."""
    camera = _camera()

    await camera.async_ptz_move("up", 5, 0.4)

    name, args, _kwargs = camera._coordinator.client.only()
    assert name == "async_ptz_move"
    assert args[0] == NUMBER, "PTZ was sent the logical channel"
    assert args == (NUMBER, PTZ_MOVE_CODES["up"], 5, 0.4)


async def test_every_direction_has_a_code():
    """The lookup is a bare subscript, so a direction the schema allows and the map
    does not would be a KeyError inside the service call."""
    for direction in PTZ_MOVE_CODES:
        camera = _camera()

        await camera.async_ptz_move(direction, 1, 0.1)

        assert camera._coordinator.client.only()[1][1] == PTZ_MOVE_CODES[direction]


async def test_going_to_a_preset_uses_the_channel_number():
    camera = _camera()

    await camera.async_goto_preset_position(4)

    assert camera._coordinator.client.only() == (
        "async_goto_preset_position", (NUMBER, 4), {})
    assert camera._coordinator.refreshed == 1


async def test_the_SDT4E425_goes_over_rpc2_on_channel_one():
    """That model's ptz.cgi does not accept GotoPreset, so it is driven over RPC2
    instead, and with a literal 1: the RPC2 path numbers its own channel and does
    not take this entity's."""
    camera = _camera(model="DH-SDT4E425-4F-GB-A-PV1")

    await camera.async_goto_preset_position(7)

    assert camera._coordinator.client.only() == (
        "async_goto_preset_rpc2", (1, 7), {})


# --- day, night and recording ----------------------------------------------

async def test_the_day_night_service_passes_the_config_type_through():
    camera = _camera()

    await camera.async_set_video_in_day_night_mode("day", "Color")

    assert camera._coordinator.client.only() == (
        "async_set_video_in_day_night_mode", (LOGICAL, "day", "Color"), {})
    assert camera._coordinator.refreshed == 1


async def test_the_record_mode_service():
    camera = _camera()

    await camera.async_set_record_mode("Manual")

    assert camera._coordinator.client.only() == (
        "async_set_record_mode", (LOGICAL, "Manual"), {})
    assert camera._coordinator.refreshed == 1


async def test_an_ordinary_camera_sets_the_video_profile_directly():
    camera = _camera()

    await camera.async_set_video_profile_mode("night")

    assert camera._coordinator.client.only() == (
        "async_set_video_profile_mode", (LOGICAL, "night"), {})


async def test_a_shape_that_cannot_select_the_profile_is_refused():
    """#458, which answered `Unknown error` from February 2025. VideoInMode comes in
    three shapes and this writes Config[0], which selects the profile in only one.

    This used to warn and write anyway, left as warn-not-refuse pending a device in
    each shape to justify refusing. That evidence is now in: on the general shape a
    DHI-NVR5464 accepts the write with 200 and keeps rendering the profile it was on,
    and the #458 reporter's camera threw on it. So the service refuses with a reason
    rather than sending a write that is ignored or errors -- and nothing is written.
    """
    camera = _camera(profile_is_writable=False)

    with pytest.raises(HomeAssistantError) as caught:
        await camera.async_set_video_profile_mode("night")

    assert camera._coordinator.client.calls == [], "a doomed write was sent anyway"
    assert caught.value.translation_key == "video_profile_not_switchable"
    assert caught.value.translation_placeholders == {
        "device": "Front Door", "shape": "general"}


async def test_an_ordinary_shape_says_nothing(caplog):
    """The control. A warning on every profile write would be noise, and would train
    people to ignore the one that matters."""
    camera = _camera()

    await camera.async_set_video_profile_mode("night")

    said = [r.getMessage() for r in caplog.records
            if r.levelname == "WARNING"
            and r.name.startswith("custom_components.dahua")]
    assert said == [], said


@pytest.mark.parametrize("model", [
    "DHI-NVR4108HS-8P-4KS2",
    "IPC-Color4K-T",
    "Lorex NVR4108HS",
])
async def test_the_models_that_switch_it_instead(model):
    """A whitelist on the model string, which is the bug class this integration
    keeps finding (#570, #676, #690). Testing it does not endorse it: it records
    which models take the other path, so that replacing the check with something
    the device reports can be shown not to change these three.
    """
    camera = _camera(model=model)

    await camera.async_set_video_profile_mode("night")

    assert camera._coordinator.client.only()[0] == "async_set_night_switch_mode"


async def test_a_model_string_that_merely_contains_a_digit_is_not_matched():
    """The check is a substring search, so the negative control is worth having."""
    camera = _camera(model="DHI-NVR4104HS-4P-4KS2")

    await camera.async_set_video_profile_mode("night")

    assert camera._coordinator.client.only()[0] == "async_set_video_profile_mode"


# --- focus, privacy and the overlays ---------------------------------------

async def test_adjusting_focus_takes_no_channel():
    """It is one of the few client calls with no channel at all, so an added one
    would be a silent argument shift."""
    camera = _camera()

    await camera.async_adjustfocus("0.5", "0.2")

    assert camera._coordinator.client.only() == (
        "async_adjustfocus_v1", ("0.5", "0.2"), {})
    assert camera._coordinator.refreshed == 1


async def test_privacy_masking_takes_an_index_and_no_channel():
    camera = _camera()

    await camera.async_set_privacy_masking(2, True)

    assert camera._coordinator.client.only() == (
        "async_setprivacymask", (2, True), {})


async def test_the_lens_privacy_mode_is_device_wide():
    """A different thing from privacy masking, and a different client call. Both
    services exist and the names are one word apart."""
    camera = _camera()

    await camera.async_set_privacy_mode(True)

    assert camera._coordinator.client.only() == (
        "async_set_privacy_mode", (True,), {})
    assert camera._coordinator.refreshed == 1


async def test_the_channel_title_overlay():
    camera = _camera()

    await camera.async_set_enable_channel_title(False)

    assert camera._coordinator.client.only() == (
        "async_enable_channel_title", (LOGICAL, False), {})


async def test_the_time_overlay():
    camera = _camera()

    await camera.async_set_enable_time_overlay(True)

    assert camera._coordinator.client.only() == (
        "async_enable_time_overlay", (LOGICAL, True), {})


async def test_the_text_overlay_carries_its_group():
    """Four overlay services, two of which take a group. Swapping the group and the
    enabled flag would be accepted by both and would do the wrong thing."""
    camera = _camera()

    await camera.async_set_enable_text_overlay(2, True)

    assert camera._coordinator.client.only() == (
        "async_enable_text_overlay", (LOGICAL, 2, True), {})


async def test_the_custom_overlay_carries_its_group():
    camera = _camera()

    await camera.async_set_enable_custom_overlay(3, False)

    assert camera._coordinator.client.only() == (
        "async_enable_custom_overlay", (LOGICAL, 3, False), {})


# --- IVS rules --------------------------------------------------------------

async def test_enabling_all_ivs_rules():
    camera = _camera()

    await camera.async_set_enable_all_ivs_rules(True)

    assert camera._coordinator.client.only() == (
        "async_set_all_ivs_rules", (LOGICAL, True), {})


async def test_enabling_one_ivs_rule_by_index():
    camera = _camera()

    await camera.async_enable_ivs_rule(1, False)

    assert camera._coordinator.client.only() == (
        "async_set_ivs_rule", (LOGICAL, 1, False), {})


# --- the doorbell services --------------------------------------------------

async def test_opening_a_door_takes_the_door_id_and_no_channel():
    """A VTO's doors are numbered by the access control module, not by video
    channel, which is why no channel is sent."""
    camera = _camera()

    await camera.async_vto_open_door(1)

    assert camera._coordinator.client.only() == (
        "async_access_control_open_door", (1,), {})


async def test_rebooting_does_not_refresh():
    """Refreshing a device that has just been told to reboot would poll something
    on its way down and log a failure for it."""
    camera = _camera()

    await camera.async_reboot()

    assert camera._coordinator.client.only() == ("reboot", (), {})
    assert camera._coordinator.refreshed == 0


# --- and the property the platform reads ------------------------------------

def test_motion_detection_status_comes_from_the_coordinator():
    assert _camera().motion_detection_enabled is True
