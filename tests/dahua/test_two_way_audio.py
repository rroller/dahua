"""Home Assistant 2026.11 can talk through a camera that says it can (#1162).

The talk button sends the browser's microphone over WebRTC to the go2rtc
inside Home Assistant, which forwards it on the RTSP backchannel of the
stream source. So the capability is offered for a doorbell, which has a
speaker, and only while the stream source leaves the backchannel open.

The test pin predates `CameraEntityCapabilities` (and the property that
reads it), so the entity tests put a stand-in in its place and read the
attribute the camera sets. What is under test is which answer the camera hands
Home Assistant, not Home Assistant's own class.
"""

from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from custom_components.dahua import DahuaDataUpdateCoordinator
from custom_components.dahua import camera as camera_module
from custom_components.dahua.camera import DahuaCamera, two_way_audio_supported


@dataclass(frozen=True, kw_only=True)
class _Capabilities:
    two_way_audio: bool = False


class _Client:
    @staticmethod
    def to_stream_name(subtype):
        return "Main" if subtype == 0 else "Sub"

    @staticmethod
    def get_rtsp_stream_url(channel, subtype):
        return "rtsp://host/cam?channel=%s&subtype=%s" % (channel, subtype)


class _Coordinator:
    client = _Client()
    channel_option = DahuaDataUpdateCoordinator.channel_option

    def __init__(self, doorbell, **options):
        self.config_entry = SimpleNamespace(
            title="Front Door", data={}, options=dict(options)
        )
        self._channel_config = {}
        self._doorbell = doorbell

    def is_doorbell(self):
        return self._doorbell

    def get_channel(self):
        return 0

    def get_channel_number(self):
        return 1

    def get_serial_number(self):
        return "SERIAL"


def _camera(doorbell, **options):
    coordinator = _Coordinator(doorbell, **options)
    return DahuaCamera(coordinator, 0, coordinator.config_entry)


@pytest.fixture
def capabilities(monkeypatch):
    monkeypatch.setattr(camera_module, "CameraEntityCapabilities", _Capabilities)


def test_a_doorbell_with_the_backchannel_open_can_talk():
    assert two_way_audio_supported(True, False) is True


def test_a_doorbell_with_the_backchannel_disabled_cannot():
    """go2rtc is told #backchannel=0, so there is nowhere for the audio to go."""
    assert two_way_audio_supported(True, True) is False


def test_a_camera_without_a_speaker_cannot():
    assert two_way_audio_supported(False, False) is False


def test_a_doorbell_offers_talking(capabilities):
    assert _camera(True)._attr_camera_entity_capabilities == _Capabilities(
        two_way_audio=True
    )


def test_the_option_takes_talking_away(capabilities):
    assert _camera(True, disable_backchannel=True)._attr_camera_entity_capabilities == (
        _Capabilities(two_way_audio=False)
    )


def test_a_camera_does_not_offer_talking(capabilities):
    assert _camera(False)._attr_camera_entity_capabilities == _Capabilities(
        two_way_audio=False
    )


def test_a_coordinator_without_is_doorbell_does_not_offer_talking(capabilities):
    camera = DahuaCamera(_NoDoorbell(), 0, _NoDoorbell.config_entry)
    assert camera._attr_camera_entity_capabilities == _Capabilities(two_way_audio=False)


class _NoDoorbell:
    client = _Client()
    channel_option = DahuaDataUpdateCoordinator.channel_option
    config_entry = SimpleNamespace(title="Cam", data={}, options={})
    _channel_config: dict = {}

    def get_channel(self):
        return 0

    def get_channel_number(self):
        return 1

    def get_serial_number(self):
        return "SERIAL"


def test_an_older_core_gets_no_capability_and_no_error(monkeypatch):
    """Before 2026.11 there is no such class; the camera still builds."""
    monkeypatch.setattr(camera_module, "CameraEntityCapabilities", None)
    camera = _camera(True)
    assert "_attr_camera_entity_capabilities" not in vars(camera)
