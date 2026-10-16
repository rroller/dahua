"""One press opens the door once, however the action is aimed (#154).

A camera entity is created per stream and an entity service runs on every
entity the call resolves to, so aiming `dahua.vto_open_door` at the **device**
sent one `openDoor` per enabled stream.

Measured on @Dieghito72's DHI-VTO2201F-P, from his own debug log, one press:

    22:08:35.855  Writing to ... accessControl.cgi?action=openDoor...
    22:08:36.216  AccessControl  Name=OpenDoor  Status=1      <- it opened
    22:08:36.220  Writing to ... accessControl.cgi?action=openDoor...
    22:08:36.289  HTTP 400 Bad Request

`call_service at pos 1` in the same log says that was one action, not two. And
the device is entitled to refuse the second one: two back-to-back `openDoor`
requests by curl answer `200` then `400 Bad Request`, with no Home Assistant
involved, while the same pair of `getSystemInfo` reads answer `200` twice.

So the user's gate opened and the user got an error, and the error belonged to
a request nobody asked for. He confirmed it by aiming the same action at one
entity instead: the gate opened with no error.

These run Home Assistant's **real** `entity_service_call`, because the thing
under test is a property of its fan-out: the same `ServiceCall` object reaching
several entities. A hand-written double would decide the answer by how it was
written.

The two shapes this rejects, each failing a different test below:

* **only the main stream acts.** Silent on a call aimed at a sub stream alone,
  which is a door that does not open and says nothing.
  -> test_a_sub_stream_asked_on_its_own_still_opens_the_door
* **dedupe on the context id.** Script steps share one `Context`, so the second
  deliberate open of a two-step script would be dropped.
  -> test_two_presses_in_one_script_open_the_door_twice
"""

import pytest

from homeassistant.core import Context, HassJob, ServiceCall
from homeassistant.helpers.service import entity_service_call

from custom_components.dahua import camera as camera_module
from custom_components.dahua.camera import DahuaCamera
from custom_components.dahua.client import _device_key

ADDRESS = "192.168.1.200"


class _Client:
    """Records the door requests, and refuses a second one as the VTO does."""

    def __init__(self, address=ADDRESS, port=80):
        self.opened = []
        # Built by the real helper rather than spelled out here, so a change to
        # how a device is identified reaches this test instead of passing it by.
        self.device_key = _device_key(address, port)

    async def async_access_control_open_door(self, door_id):
        self.opened.append(door_id)
        if len(self.opened) > 1:
            raise AssertionError(
                "the doorbell answered 400 Bad Request to this second openDoor; "
                "one press must send one request"
            )

    def get_rtsp_stream_url(self, channel, subtype):
        return "rtsp://%s/cam/realmonitor?channel=%s&subtype=%s" % (
            ADDRESS,
            channel,
            subtype,
        )

    def to_stream_name(self, index):
        return "Main" if index == 0 else "Sub"


class _Coordinator:
    """Enough doorbell for the entity, with one client shared by both streams.

    Shared on purpose: the two camera entities of one doorbell talk to the same
    device, which is the whole reason a second request reaches it at all.
    """

    def __init__(self, client, address=ADDRESS):
        self.client = client
        self._address = address
        self._channel = 0
        self.data = {}
        # Read by CoordinatorEntity.available, which the real fan-out filters on.
        self.last_update_success = True

    def get_address(self):
        return self._address

    def get_device_name(self):
        return "Cancello"

    def get_channel(self):
        return 0

    def get_channel_number(self):
        return 1

    def get_serial_number(self):
        return "8D03E2FPAJ9E513"

    def get_model(self):
        return "DHI-VTO2201F-P"

    def get_firmware_version(self):
        return "1.00"

    def get_device_type(self):
        return "DHI-VTO2201F-P"

    def is_doorbell(self):
        return True

    def get_stream_codec(self, index):
        return None

    def get_stream_has_audio(self, index):
        return False

    def channel_option(self, key, default=None):
        return default

    @property
    def subentry_id(self):
        return None


def _cameras(hass, name="cancello", address=ADDRESS, port=80):
    """A doorbell's main and sub camera entities, both enabled.

    Both, because a sub stream is created disabled by default
    (`entity_registry_enabled_default` is `self._stream_index == 0`) and this
    bug only reaches a user who turned theirs on, which @Dieghito72 had.
    """
    client = _Client(address, port)
    coordinator = _Coordinator(client, address)
    entry = type("Entry", (), {"entry_id": "an-entry", "data": {}, "options": {}})()

    cameras = {}
    for index, suffix in ((0, "main"), (1, "sub")):
        entity = DahuaCamera(coordinator, index, entry)
        entity.hass = hass
        entity.entity_id = "camera.%s_%s" % (name, suffix)
        cameras[entity.entity_id] = entity
    return client, cameras


def _call(hass, entity_ids, context=None, door_id=1):
    """The service call Home Assistant builds, with its targets in `data`."""
    data = {"door_id": door_id}
    if entity_ids is not None:
        data["entity_id"] = entity_ids
    return ServiceCall(
        hass,
        "dahua",
        "vto_open_door",
        data,
        context=context or Context(),
    )


async def _fan_out(hass, cameras, call):
    """Home Assistant's own entity service dispatch, not a stand-in.

    The `HassJob` wrapper is what `async_register_entity_service` puts round a
    callable handler, so this is the same thing the platform registers.
    """
    return await entity_service_call(
        hass,
        cameras,
        HassJob(camera_module._async_open_door_once),
        call,
    )


@pytest.fixture(autouse=True)
def _forget_previous_calls():
    """The store is module level, so one test must not answer for the next."""
    camera_module._OPEN_DOOR_CALLS.clear()
    yield
    camera_module._OPEN_DOOR_CALLS.clear()


async def test_one_action_aimed_at_both_cameras_opens_the_door_once(hass):
    """What targeting the device does: every enabled camera of it is called."""
    client, cameras = _cameras(hass)

    await _fan_out(
        hass,
        cameras,
        _call(hass, ["camera.cancello_main", "camera.cancello_sub"]),
    )

    assert client.opened == [1], "one press, one openDoor"


async def test_a_sub_stream_asked_on_its_own_still_opens_the_door(hass):
    """A door that silently does nothing is worse than the bug being fixed."""
    client, cameras = _cameras(hass)

    await _fan_out(hass, cameras, _call(hass, ["camera.cancello_sub"]))

    assert client.opened == [1]


async def test_two_presses_in_one_script_open_the_door_twice(hass):
    """Script steps share a Context, so the context cannot tell them apart.

    Two deliberate opens are two actions and must send two requests, which is
    why this is keyed on the call object rather than on `call.context.id`.
    """
    client, cameras = _cameras(hass)
    shared = Context()

    first = _call(hass, ["camera.cancello_main", "camera.cancello_sub"], shared)
    second = _call(hass, ["camera.cancello_main", "camera.cancello_sub"], shared)
    assert first.context.id == second.context.id, "the premise of this test"

    await _fan_out(hass, cameras, first)
    client.opened.clear()
    await _fan_out(hass, cameras, second)

    assert client.opened == [1], "the second press must reach the device"


async def test_the_door_id_asked_for_is_the_one_sent(hass):
    """The call carries its target fields too, so the door is read by name."""
    client, cameras = _cameras(hass)

    await _fan_out(
        hass,
        cameras,
        _call(hass, ["camera.cancello_main", "camera.cancello_sub"], door_id=2),
    )

    assert client.opened == [2]


async def test_two_doorbells_in_one_call_each_open_once(hass):
    """The store is keyed per device, not per call.

    An action aimed at an area holding two doorbells must open both, once each.
    """
    client_a, cameras_a = _cameras(hass)
    client_b, cameras_b = _cameras(hass, "portone", "192.168.1.201")

    cameras = {**cameras_a, **cameras_b}
    await _fan_out(hass, cameras, _call(hass, list(cameras)))

    assert client_a.opened == [1]
    assert client_b.opened == [1]


async def test_two_doorbells_behind_one_address_each_open_once(hass):
    """Two boxes on one IP, different ports, are two devices.

    A bridge forwarding 80 to one doorbell and 8081 to another is what
    `_device_key` in client.py exists for, and it is a supported setup. Keyed on
    the address alone, the second doorbell's entities all take themselves for
    the first one's siblings, so that gate never opens and nothing is raised:
    the action reports success and the visitor stays outside.
    """
    client_a, cameras_a = _cameras(hass, port=80)
    client_b, cameras_b = _cameras(hass, "portone", port=8081)
    assert client_a.device_key != client_b.device_key, "the premise of this test"
    assert (
        cameras_a["camera.cancello_main"]._coordinator.get_address()
        == cameras_b["camera.portone_main"]._coordinator.get_address()
    ), "and they really do share an address"

    cameras = {**cameras_a, **cameras_b}
    await _fan_out(hass, cameras, _call(hass, list(cameras)))

    assert client_a.opened == [1]
    assert client_b.opened == [1], "the second doorbell is a different door"


async def test_the_entity_method_is_untouched_by_the_wrapper(hass):
    """Anything calling the method directly keeps the behaviour it had."""
    client, cameras = _cameras(hass)

    await cameras["camera.cancello_sub"].async_vto_open_door(3)

    assert client.opened == [3]
    assert camera_module._OPEN_DOOR_CALLS == {}, "no call to remember"


def test_the_service_is_registered_with_the_wrapper():
    """Registered by name, Home Assistant hands over data and not the call.

    Which is the whole mechanism: `entity_service_call` passes the `ServiceCall`
    itself only when the registered handler is a callable.
    """
    import ast
    import pathlib

    source = (
        pathlib.Path(__file__).resolve().parents[2]
        / "custom_components"
        / "dahua"
        / "camera.py"
    ).read_text(encoding="utf-8")

    for node in ast.walk(ast.parse(source)):
        if not (
            isinstance(node, ast.Call)
            and getattr(node.func, "attr", None) == "async_register_entity_service"
            and len(node.args) >= 3
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id == "SERVICE_VTO_OPEN_DOOR"
        ):
            continue
        handler = node.args[2]
        assert isinstance(handler, ast.Name), (
            "vto_open_door must be registered with a callable, not the method "
            "name, or Home Assistant hands over call.data and the fan-out "
            "cannot be told apart"
        )
        assert handler.id == "_async_open_door_once"
        return
    raise AssertionError("SERVICE_VTO_OPEN_DOOR is not registered")
