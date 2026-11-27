"""A camera that says it has an audio output gets Home Assistant's talk button (#177).

The button was offered on doorbells only (#1162). @felipecrs asked whether a
camera with a speaker could have it too, and whether
`devAudioOutput.cgi?action=getCollect` could decide that. It can only decide it
honestly if the answer agrees with the way Home Assistant sends the audio,
which is the RTSP backchannel. @kmich measured both halves on two cameras:

| camera | `getCollect` | DESCRIBE asking for the backchannel |
| --- | --- | --- |
| DH-IPC-HDW3549H-AS-PV, has a speaker | `result=1` | 200, with a `sendonly` PCMA track |
| IPC-HDW2549TM-S-IL, microphone only | `result=0` | 404 |

Also measured, `result=1`: a VTO2000A, a VTO2202F, a VTO2211G and a
DHI-NVR5464-16P-EI. The recorder is why only a directly connected camera is
asked: it answers for its own audio output, and nobody has measured whether
talking reaches the camera behind one of its channels.

Not measured: anybody talking through a camera from Home Assistant. What is
measured is that the camera describes the track Home Assistant would send on.

Reintroduced one at a time against the real source:

| reintroduced | caught by |
| --- | --- |
| the read asking a different URL | `the_question_is_one_exact_request` |
| an answer without a number read as none | `an_answer_without_a_number_is_not_a_no` |
| the probe judging on the read not raising | `a_camera_that_says_it_has_none_gets_no_button` |
| a recorder asked | `a_recorder_is_not_asked` |
| a doorbell or a monitor asked | `a_doorbell_and_a_monitor_are_not_asked` |
| silence recorded as an answer | `silence_leaves_the_question_open` |
| the registry entry removed | `an_answer_that_arrives_late_brings_the_button` |
| `supports_talking` not asking again what the device is | `a_class_that_arrives_late_takes_the_button_back` |
| a doorbell losing the button | `a_doorbell_is_talked_to_whatever_else_is_known` |
| the camera reading the answer once, at build | `the_button_turns_up_on_the_stream_that_already_exists` |
| the option ignored for a camera | `the_option_takes_it_away_from_a_camera_too` |
| the setup block not asking | `setup_asks_only_where_the_camera_platform_is_loaded` |
"""

import inspect
from dataclasses import dataclass
from types import SimpleNamespace

import pytest
from aiohttp import ClientResponseError

from custom_components.dahua import DahuaDataUpdateCoordinator
from custom_components.dahua import camera as camera_module
from custom_components.dahua import coordinator as coordinator_module
from custom_components.dahua.camera import DahuaCamera
from custom_components.dahua.client import DahuaClient
from custom_components.dahua.coordinator import RETRYABLE_CAPABILITIES

ADDRESS = "192.168.1.108"
SERIAL = "AB0000000000C1"


# --- the read ----------------------------------------------------------------


class _Wire:
    """`DahuaClient.get`, recording what it was asked."""

    def __init__(self, answer):
        self._answer = answer
        self.asked = []

    async def __call__(self, url, verify_ok=False):
        self.asked.append(url)
        if isinstance(self._answer, Exception):
            raise self._answer
        return dict(self._answer)


def _client(answer):
    client = object.__new__(DahuaClient)
    client.get = _Wire(answer)
    return client


async def test_the_question_is_one_exact_request():
    client = _client({"result": "1"})

    assert await client.async_get_audio_output_count() == 1
    assert client.get.asked == ["/cgi-bin/devAudioOutput.cgi?action=getCollect"]


@pytest.mark.parametrize(
    "answer, count",
    [({"result": "1"}, 1), ({"result": "0"}, 0), ({"result": " 2 "}, 2)],
)
async def test_the_number_is_what_the_device_said(answer, count):
    assert await _client(answer).async_get_audio_output_count() == count


@pytest.mark.parametrize("answer", [{}, {"result": ""}, {"result": "Error"}])
async def test_an_answer_without_a_number_is_not_a_no(answer):
    """0 is the device saying it has none. This is the device saying nothing
    that can be read, and the two must not come out the same."""
    assert await _client(answer).async_get_audio_output_count() is None


async def test_a_refusal_reaches_the_caller():
    """So the probe can tell a 400 from a timeout."""
    refusal = ClientResponseError(None, None, status=400)

    with pytest.raises(ClientResponseError):
        await _client(refusal).async_get_audio_output_count()


# --- what the probe concludes ------------------------------------------------


class _Device:
    """The one read, as the test dictates. The last answer repeats."""

    def __init__(self, answers):
        self._answers = list(answers)
        self.reads = 0

    async def async_get_audio_output_count(self):
        self.reads += 1
        answer = self._answers[0] if len(self._answers) == 1 else self._answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def _coordinator(*answers, device_class="IPC", model="DH-IPC-HDW3549H-AS-PV"):
    """A real coordinator's probe, accessor and what-is-this-device methods,
    with only the device behind them replaced."""
    coordinator = object.__new__(DahuaDataUpdateCoordinator)
    coordinator.client = _Device(answers or (1,))
    coordinator._channel = 0
    coordinator._address = ADDRESS
    coordinator._device_class = device_class
    coordinator.model = model
    coordinator._nvr_active_deterrence = False
    coordinator._supports_audio_output = False
    coordinator._probe_refusals = {}
    coordinator._capability_retries = {}
    coordinator.data = {}
    coordinator.hass = None
    coordinator.config_entry = SimpleNamespace(
        entry_id="entry-1", title="Camera", data={}, options={}
    )
    coordinator._channel_config = {}
    coordinator.get_serial_number = lambda: SERIAL
    return coordinator


@pytest.fixture
def discoveries(monkeypatch):
    announced = []
    monkeypatch.setattr(
        coordinator_module,
        "async_notify_capability_discovered",
        lambda hass, entry_id, channel_key: announced.append((entry_id, channel_key)),
    )
    return announced


async def test_a_camera_that_says_it_has_one_can_be_talked_to():
    """The DH-IPC-HDW3549H-AS-PV."""
    coordinator = _coordinator(1)

    assert await coordinator._async_probe_audio_output() is True
    assert coordinator.supports_talking() is True


async def test_a_camera_that_says_it_has_none_gets_no_button():
    """The IPC-HDW2549TM-S-IL: the read succeeds, and the answer is no."""
    coordinator = _coordinator(0)

    assert await coordinator._async_probe_audio_output() is False
    assert coordinator.supports_talking() is False
    assert coordinator.probes_that_did_not_answer() == set()


async def test_an_answer_nobody_can_read_gets_no_button():
    coordinator = _coordinator(None)

    assert await coordinator._async_probe_audio_output() is False
    assert coordinator.supports_talking() is False


@pytest.mark.parametrize("device_class", ["NVR", "DVR", "XVR", "HCVR"])
async def test_a_recorder_is_not_asked(device_class):
    """It would answer 1 for its own audio output, on every channel."""
    coordinator = _coordinator(1, device_class=device_class, model="DHI-NVR5464")

    assert await coordinator._async_probe_audio_output() is False
    assert coordinator.client.reads == 0
    assert coordinator.supports_talking() is False
    assert coordinator.probes_that_did_not_answer() == set()


@pytest.mark.parametrize(
    "device_class, model", [("VTO", "VTO2000A"), ("VTH", "VTH5221D")]
)
async def test_a_doorbell_and_a_monitor_are_not_asked(device_class, model):
    coordinator = _coordinator(1, device_class=device_class, model=model)

    await coordinator._async_probe_audio_output()

    assert coordinator.client.reads == 0
    assert coordinator._supports_audio_output is False


async def test_a_doorbell_is_talked_to_whatever_else_is_known():
    """As before this change, and without a request."""
    coordinator = _coordinator(0, device_class="VTO", model="VTO2000A")

    assert coordinator.supports_talking() is True


async def test_a_monitor_is_not():
    coordinator = _coordinator(1, device_class="VTH", model="VTH5221D")
    coordinator._supports_audio_output = True

    assert coordinator.supports_talking() is False


async def test_a_class_that_arrives_late_takes_the_button_back():
    """A device that had not said what it is was asked as a camera and said
    1. When its class then arrives and it is a recorder, that 1 was about the
    recorder."""
    coordinator = _coordinator(1, device_class="", model="N843A8")
    await coordinator._async_probe_audio_output()
    assert coordinator.supports_talking() is True

    coordinator._device_class = "NVR"

    assert coordinator.supports_talking() is False


# --- silence, and the retry --------------------------------------------------


async def test_silence_leaves_the_question_open():
    coordinator = _coordinator(TimeoutError())

    assert await coordinator._async_probe_audio_output() is False
    assert coordinator.supports_talking() is False
    assert coordinator.probes_that_did_not_answer() == {"audio_output"}


async def test_a_refusal_is_an_answer():
    """A camera without the call says so with a status, and is not asked again."""
    coordinator = _coordinator(ClientResponseError(None, None, status=400))

    assert await coordinator._async_probe_audio_output() is False
    assert coordinator.probes_that_did_not_answer() == set()


async def test_an_answer_that_arrives_late_brings_the_button(discoveries):
    """Through the real registry, so removing the entry fails this."""
    assert "audio_output" in RETRYABLE_CAPABILITIES
    coordinator = _coordinator(TimeoutError(), 1)
    await coordinator._async_probe_audio_output()
    assert coordinator.supports_talking() is False

    await coordinator._async_retry_unanswered_capabilities()

    assert coordinator.supports_talking() is True
    assert coordinator.client.reads == 2


async def test_a_late_no_is_not_asked_a_third_time(discoveries):
    coordinator = _coordinator(TimeoutError(), 0)
    await coordinator._async_probe_audio_output()

    await coordinator._async_retry_unanswered_capabilities()
    await coordinator._async_retry_unanswered_capabilities()

    assert coordinator.client.reads == 2
    assert coordinator.supports_talking() is False
    assert discoveries == []


def test_setup_asks_only_where_the_camera_platform_is_loaded():
    """Nothing else reads the answer. Read from the source because the setup
    block cannot be run without a device."""
    source = inspect.getsource(DahuaDataUpdateCoordinator._async_update_data)
    asked = source.index("await self._async_probe_audio_output()")
    gate = source.rindex("if self._wanted_by(CAMERA):", 0, asked)

    assert source[gate:asked].count("\n") == 1, "the gate is not the line above"
    assert source.count("await self._async_probe_audio_output()") == 1


# --- the camera --------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class _Capabilities:
    two_way_audio: bool = False


class _StreamClient:
    @staticmethod
    def to_stream_name(subtype):
        return "Main" if subtype == 0 else "Sub"

    @staticmethod
    def get_rtsp_stream_url(channel, subtype):
        return "rtsp://host/cam?channel=%s&subtype=%s" % (channel, subtype)


@pytest.fixture
def capabilities(monkeypatch):
    monkeypatch.setattr(camera_module, "CameraEntityCapabilities", _Capabilities)


def _camera(coordinator, **options):
    coordinator.config_entry.options.update(options)
    coordinator.client = _StreamClient()
    coordinator.get_channel = lambda: 0
    coordinator.get_channel_number = lambda: 1
    return DahuaCamera(coordinator, 0, coordinator.config_entry)


def _talks(camera):
    return camera._attr_camera_entity_capabilities.two_way_audio


async def test_a_camera_with_an_audio_output_offers_talking(capabilities):
    coordinator = _coordinator(1)
    await coordinator._async_probe_audio_output()

    assert _talks(_camera(coordinator)) is True


async def test_a_camera_without_one_does_not(capabilities):
    coordinator = _coordinator(0)
    await coordinator._async_probe_audio_output()

    assert _talks(_camera(coordinator)) is False


async def test_the_option_takes_it_away_from_a_camera_too(capabilities):
    """go2rtc is told #backchannel=0, so there is nowhere for the audio to go."""
    coordinator = _coordinator(1)
    await coordinator._async_probe_audio_output()

    assert _talks(_camera(coordinator, disable_backchannel=True)) is False


async def test_the_button_turns_up_on_the_stream_that_already_exists(
    capabilities, monkeypatch
):
    """The camera entity is built at setup, when this camera had not answered.
    The answer arrives on a later poll, and no entity is built for it."""
    coordinator = _coordinator(TimeoutError(), 1)
    await coordinator._async_probe_audio_output()
    camera = _camera(coordinator)
    assert _talks(camera) is False
    written = []
    monkeypatch.setattr(camera, "async_write_ha_state", lambda: written.append(1))

    coordinator._supports_audio_output = True
    camera._handle_coordinator_update()

    assert _talks(camera) is True
    assert written == [1], "the poll's own state write was lost"


async def test_a_poll_that_changes_nothing_hands_over_nothing_new(
    capabilities, monkeypatch
):
    """Assigning the capability makes Home Assistant drop the one it holds, so
    the same answer is the same object."""
    coordinator = _coordinator(1)
    await coordinator._async_probe_audio_output()
    camera = _camera(coordinator)
    monkeypatch.setattr(camera, "async_write_ha_state", lambda: None)
    held = camera._attr_camera_entity_capabilities

    camera._handle_coordinator_update()

    assert camera._attr_camera_entity_capabilities is held
