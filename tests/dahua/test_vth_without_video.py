"""An indoor monitor (VTH) without a camera gets no camera entities.

Every device got one camera entity per stream, whatever it was. A VTH2421F-P has no
camera, and says so in its own entry of its RemoteDevice table, read over RPC2:

    configManager.getConfig {"name": "RemoteDevice"}
      table.Local0 = {"DeviceClass": "VTH", "SupportVideo": false,
                      "VideoInputChannels": 1, ...}

Some indoor monitors do have a camera, so being a VTH is not enough: only a VTH that
says SupportVideo false loses its cameras, and any other answer, or none, leaves them
exactly as they were. `VideoInputChannels` is 1 on the measured VTH anyway, which is why
it is not the signal.

The table lists the other devices the VTH knows with their credentials, so the only
thing taken from it is that one flag.
"""
import aiohttp
import pytest

from custom_components.dahua import DahuaDataUpdateCoordinator
from custom_components.dahua import camera as camera_module
from custom_components.dahua.client import DahuaClient
from custom_components.dahua.rpc2 import Rpc2MethodRefused

from . import adds_entities
from .test_camera_setup import _Coordinator, _Entry, _Platform
from .test_vth_identity_over_rpc2 import VTH_ANSWERS, _Rpc2


def _remote_device(local0):
    return {"result": True, "params": {"table": {
        "Local0": local0,
        "Vto00": {"Address": "10.0.0.4", "DeviceClass": "VTO", "SupportVideo": True,
                  "UserName": "admin", "Password": "secret"},
    }}}


VTH_LOCAL0 = {"Address": "127.0.0.1", "DeviceClass": "VTH", "SupportVideo": False,
              "VideoInputChannels": 1}


def _client(remote_device=None, cgi_status=404):
    client = DahuaClient("u", "p", "10.0.0.17", 80, 554, None)
    answers = dict(VTH_ANSWERS)
    answers["configManager.getConfig"] = (
        _remote_device(VTH_LOCAL0) if remote_device is None else remote_device)
    rpc2 = _Rpc2(answers)

    async def get(url, verify_ok=False):
        raise aiohttp.ClientResponseError(None, None, status=cgi_status)

    async def shared_call(action):
        return await action(rpc2)

    client.get = get
    client._rpc2_shared_call = shared_call
    return client, rpc2


# --- what the VTH says about itself ------------------------------------------

async def test_a_vth_that_says_it_has_no_camera_is_without_video():
    client, rpc2 = _client()

    await client.get_device_type()

    assert client.vth_own_video() is False
    assert ("configManager.getConfig", {"name": "RemoteDevice"}) in rpc2.asked


async def test_a_vth_that_says_it_has_a_camera_keeps_it():
    client, _ = _client(_remote_device(dict(VTH_LOCAL0, SupportVideo=True)))

    await client.get_device_type()

    assert client.vth_own_video() is True


@pytest.mark.parametrize("local0", [
    {k: v for k, v in VTH_LOCAL0.items() if k != "SupportVideo"},   # not said
    dict(VTH_LOCAL0, SupportVideo="false"),                           # a string, not a bool
    dict(VTH_LOCAL0, SupportVideo=0),                                 # nor a number
    dict(VTH_LOCAL0, DeviceClass="VTO"),                              # not this VTH's entry
    "not a table",
])
async def test_anything_but_a_plain_answer_is_no_answer(local0):
    client, _ = _client(_remote_device(local0))

    await client.get_device_type()

    assert client.vth_own_video() is None


async def test_a_vth_with_no_remote_device_table_keeps_its_identity():
    """The camera question is extra. Failing it must not cost the VTH its model."""
    client, _ = _client(Rpc2MethodRefused("refused"))

    assert await client.get_device_type() == {"type": "VTH2421F-P"}
    assert client.vth_own_video() is None


async def test_only_the_flag_is_kept_from_a_table_full_of_credentials():
    client, _ = _client()

    await client.get_device_type()

    assert client._vth_identity["own_video"] is False
    assert "secret" not in repr(client._vth_identity)


async def test_asking_whether_it_has_video_never_starts_an_rpc2_conversation():
    """An ordinary camera reaches this too, from the camera platform. It reports what
    the identity questions found and asks nothing itself."""
    client, rpc2 = _client()

    assert client.vth_own_video() is None
    assert rpc2.asked == []


async def test_a_device_that_is_not_a_vth_is_never_asked_for_its_table():
    answers = dict(VTH_ANSWERS)
    answers["magicBox.getDeviceClass"] = {"result": True, "params": {"type": "VTO"}}
    client, rpc2 = _client()
    rpc2.answers = dict(answers, **{"configManager.getConfig": _remote_device(VTH_LOCAL0)})

    await client.get_device_type()

    assert client.vth_own_video() is None
    assert all(method != "configManager.getConfig" for method, _ in rpc2.asked)


# --- the coordinator's question ----------------------------------------------

class _ClientSays:
    def __init__(self, own_video):
        self._own_video = own_video

    def vth_own_video(self):
        return self._own_video


def _coordinator(device_class, own_video):
    c = object.__new__(DahuaDataUpdateCoordinator)
    c._device_class = device_class
    c.client = _ClientSays(own_video)
    return c


def test_a_vth_without_a_camera_is_without_video():
    assert _coordinator("VTH", False).is_indoor_monitor_without_video()


@pytest.mark.parametrize("device_class, own_video", [
    ("VTH", True),     # a VTH with a camera
    ("VTH", None),     # a VTH that did not say
    ("VTO", False),    # not a VTH, whatever the flag
    ("IPC", False),
    ("", False),       # no class answer
    ("VTHX", False),   # the neighbouring-but-wrong class
])
def test_anything_else_keeps_its_cameras(device_class, own_video):
    assert not _coordinator(device_class, own_video).is_indoor_monitor_without_video()


def test_the_class_is_folded_like_everywhere_else():
    assert _coordinator(" vth ", False).is_indoor_monitor_without_video()


def test_a_client_without_the_question_keeps_the_cameras():
    c = object.__new__(DahuaDataUpdateCoordinator)
    c._device_class = "VTH"
    c.client = object()
    assert not c.is_indoor_monitor_without_video()


def test_a_coordinator_without_a_class_keeps_the_cameras():
    c = object.__new__(DahuaDataUpdateCoordinator)
    c.client = _ClientSays(False)
    assert not c.is_indoor_monitor_without_video()


# --- the camera platform -------------------------------------------------------

class _Vth(_Coordinator):
    def __init__(self, without_video, **kwargs):
        super().__init__(model="VTH2421F-P", **kwargs)
        self._without_video = without_video

    def is_indoor_monitor_without_video(self):
        return self._without_video


@pytest.fixture
def setup(monkeypatch):
    built = []
    platform = _Platform()

    class _Recorder:
        def __init__(self, coordinator, stream_index, config_entry, **kwargs):
            built.append((coordinator, stream_index))

    monkeypatch.setattr(camera_module, "DahuaCamera", _Recorder)
    monkeypatch.setattr(camera_module.entity_platform, "async_get_current_platform",
                        lambda: platform)

    async def run(*coordinators):
        entry = _Entry({i: c for i, c in enumerate(coordinators)})
        await camera_module.async_setup_entry(None, entry, adds_entities([]))

    run.built = built
    run.platform = platform
    return run


async def test_a_vth_without_video_gets_no_camera_entities(setup):
    await setup(_Vth(True, max_streams=3))

    assert setup.built == []


async def test_a_vth_with_video_gets_its_streams(setup):
    await setup(_Vth(False, max_streams=3))

    assert [index for _, index in setup.built] == [0, 1, 2]


async def test_the_services_are_still_registered_without_any_camera(setup):
    """They are platform-wide, and an entry with only a VTH must not be the one setup
    that never registers them."""
    await setup(_Vth(True, max_streams=3))

    assert "vto_call" in setup.platform.registered


async def test_one_channel_without_video_does_not_take_the_others_cameras(setup):
    camera = _Coordinator(max_streams=2, channel=1)
    await setup(_Vth(True, max_streams=3), camera)

    assert [(c, index) for c, index in setup.built] == [(camera, 0), (camera, 1)]


# --- and the camera features it does not have, measured on the hardware -------
#
# Added to Home Assistant, a VTH2421F-P (no camera) still got the camera events the
# add-device form offers every device. It answered none of the nine over RPC2, so
# its event stream ended with "reports none of the selected event types" and was
# retried for ever, and each event got a sensor that could never change. It also
# answers a VideoInOptions table, so it got a Day/Night select.

CAMERA_EVENTS = ["VideoMotion", "CrossLineDetection", "AlarmLocal", "VideoLoss",
                 "VideoBlind", "AudioMutation", "CrossRegionDetection",
                 "SmartMotionHuman", "SmartMotionVehicle"]


def _monitor(device_class="VTH", own_video=False, day_night=True):
    c = _coordinator(device_class, own_video)
    c.events = list(CAMERA_EVENTS)
    c._supports_day_night_color = day_night
    c._address = "10.0.0.17"
    return c


def test_a_vth_without_a_camera_has_no_camera_events():
    assert _monitor().get_event_list() == []


@pytest.mark.parametrize("device_class, own_video", [
    ("VTH", True), ("VTH", None), ("IPC", False), ("", False)])
def test_everything_else_keeps_the_events_it_was_given(device_class, own_video):
    assert _monitor(device_class, own_video).get_event_list() == CAMERA_EVENTS


async def test_no_event_stream_is_started_for_it(monkeypatch):
    from custom_components.dahua import coordinator as coordinator_module

    registered = []
    monkeypatch.setattr(coordinator_module, "_host_stream",
                        lambda hass, address: type("S", (), {
                            "register": staticmethod(registered.append)})())
    monitor = _monitor()
    monitor.hass = object()

    await monitor.async_start_event_listener()

    assert registered == []


async def test_a_camera_still_starts_its_event_stream(monkeypatch):
    from custom_components.dahua import coordinator as coordinator_module

    registered = []
    monkeypatch.setattr(coordinator_module, "_host_stream",
                        lambda hass, address: type("S", (), {
                            "register": staticmethod(registered.append)})())
    camera = _monitor("IPC", False)
    camera.hass = object()

    await camera.async_start_event_listener()

    assert registered == [camera]


def test_a_vth_without_a_camera_has_no_day_night_mode():
    assert not _monitor().supports_day_night_color()


def test_a_vth_with_a_camera_keeps_it():
    assert _monitor("VTH", True).supports_day_night_color()


def test_a_camera_that_answered_the_probe_keeps_it():
    assert _monitor("IPC", None).supports_day_night_color()


@pytest.mark.parametrize("answer, expected", [
    ("VTO", "VTO"), (" vto ", "VTO"), ("", ""), (None, "")])
def test_the_reported_class_is_only_the_devices_own_answer(answer, expected):
    c = object.__new__(DahuaDataUpdateCoordinator)
    if answer is not None:
        c._device_class = answer
    assert c.reported_device_class() == expected


DAY_NIGHT_READ = "async_get_video_in_options"


async def _poll_calls(own_video):
    from .test_poll_skips_unused import _coordinator as poll_coordinator

    c = poll_coordinator()
    c._device_class = "VTH"
    c.client.vth_own_video = lambda: own_video
    await c._async_update_data()
    return c.client.calls


async def test_the_poll_does_not_read_day_night_for_a_vth_without_a_camera():
    """The poll asks the capability rather than the probe's raw answer, which the
    VTH's VideoInOptions table passes."""
    assert DAY_NIGHT_READ not in await _poll_calls(own_video=False)


async def test_but_does_for_a_vth_with_one():
    assert DAY_NIGHT_READ in await _poll_calls(own_video=True)
