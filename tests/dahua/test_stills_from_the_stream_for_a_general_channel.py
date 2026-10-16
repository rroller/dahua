"""A channel the recorder reaches as generic RTSP serves no snapshot.

`is_onvif_channel` records the same shape of problem for ONVIF: snapshot.cgi
answers 400 for the ONVIF channel of a DHI-NVR5464-16P-EI while every
Dahua-protocol channel on the same recorder returns an image. Measured on a
DHI-NVR5232-16P-EI, whose channel 2 is a third-party camera with
`ProtocolType=General`:

    index 1  ch=2  General  snapshot.cgi -> 500 after 10-15s, no image
    index 0  ch=1  Private  snapshot.cgi -> 200, 945,557 bytes
    index 2  ch=3  Private  snapshot.cgi -> 200, 734,898 bytes

MAX_CONCURRENT_REQUESTS_PER_HOST is 2, so that one channel holds half the host's
budget for those seconds: all 14 stills requested together returned 10/14, and
14/14 once those two entities stopped asking. The channel's RTSP is fine --
`hevc 2592x1944` on main, `hevc 800x600` on sub -- so the still comes from there.

ONVIF is deliberately *not* included: its RTSP path times out as well, so it
would trade a snapshot that fails fast for a stream worker that never produces a
frame.
"""

from types import SimpleNamespace

import pytest

from homeassistant.components.camera import CameraEntityFeature

from custom_components.dahua.camera import (
    DahuaCamera,
    _stills_come_from_the_stream,
)


class _Client:
    @staticmethod
    def to_stream_name(subtype):
        return "Main" if subtype == 0 else "Sub"

    @staticmethod
    def get_rtsp_stream_url(channel, subtype):
        return "rtsp://host/cam?channel=%s&subtype=%s" % (channel, subtype)


class _Coordinator:
    """A channel that reports whatever protocol the test gives it."""

    client = _Client()

    def __init__(self, protocol="private"):
        self._protocol = protocol

    def get_channel_protocol(self):
        return self._protocol

    def get_channel(self):
        return 1

    def get_channel_number(self):
        return 2

    def get_serial_number(self):
        return "SERIAL"

    def channel_option(self, name, default=None):
        return default


def _entry():
    return SimpleNamespace(title="Recorder", data={}, options={})


# --- which channels the rule picks -------------------------------------------


def test_a_generic_rtsp_channel_is_chosen():
    assert _stills_come_from_the_stream(_Coordinator("general")) is True


def test_the_recorders_own_protocol_is_not():
    assert _stills_come_from_the_stream(_Coordinator("private")) is False


def test_an_onvif_channel_is_not():
    """Its RTSP times out too, so the stream is no better than the snapshot."""
    assert _stills_come_from_the_stream(_Coordinator("onvif")) is False


def test_a_channel_that_reported_nothing_is_not():
    """A standalone camera, a recorder that would not answer, or a failed read."""
    assert _stills_come_from_the_stream(_Coordinator(None)) is False


@pytest.mark.parametrize("protocol", ["", "   ", "Private", "ONVIF", "nonsense"])
def test_nothing_else_is_chosen(protocol):
    """A spelling nobody has measured keeps the behaviour it has today."""
    assert _stills_come_from_the_stream(_Coordinator(protocol)) is False


# --- what the entity reports --------------------------------------------------


@pytest.mark.parametrize("stream_stills", [True, False])
def test_the_entity_reports_what_setup_decided(stream_stills):
    camera = DahuaCamera(_Coordinator(), 0, _entry(), stream_stills=stream_stills)

    assert camera.use_stream_for_stills is stream_stills


def test_a_camera_asks_for_a_snapshot_unless_told_otherwise():
    """The default, so nothing that constructs one positionally changes."""
    assert DahuaCamera(_Coordinator(), 0, _entry()).use_stream_for_stills is False


# --- the MJPEG path, which Home Assistant does not route for us ---------------
#
# `handle_async_still_stream` hands an image callback to
# `async_get_still_stream` without consulting `use_stream_for_stills`, and
# `handle_async_mjpeg_stream` is built on it. These hold the override that fixes
# it: the regression is silent -- a blank response and a wasted recorder request
# per frame -- so it needs a test rather than an eye on it.


def _camera(stream_stills, monkeypatch, image=b"from-the-stream"):
    """A camera whose two image sources are distinguishable."""
    camera = DahuaCamera(
        _Coordinator("general" if stream_stills else "private"),
        0,
        _entry(),
        stream_stills=stream_stills,
    )
    camera.hass = None
    monkeypatch.setattr(
        type(camera),
        "webrtc_provider",
        property(lambda self: None),
        raising=False,
    )
    camera.stream = SimpleNamespace(
        async_get_image=_recording_stream(camera, image),
    )
    return camera


def _recording_stream(camera, image):
    async def async_get_image(width=None, height=None, wait_for_next_keyframe=False):
        camera.stream_calls = getattr(camera, "stream_calls", 0) + 1
        camera.stream_keyframe_waits = wait_for_next_keyframe
        return image

    return async_get_image


async def test_the_mjpeg_callback_renders_from_the_stream(monkeypatch):
    """The regression rroller reported: this used to ask snapshot.cgi."""
    captured = {}

    async def _fake_still_stream(request, image_cb, content_type, interval):
        captured["image"] = await image_cb()
        return "response"

    monkeypatch.setattr(
        "custom_components.dahua.camera.async_get_still_stream", _fake_still_stream
    )
    camera = _camera(True, monkeypatch)

    async def _never(*args, **kwargs):
        raise AssertionError("snapshot.cgi must not be asked for this channel")

    monkeypatch.setattr(camera, "async_camera_image", _never)

    assert await camera.handle_async_still_stream(object(), 0.5) == "response"
    assert captured["image"] == b"from-the-stream"
    assert camera.stream_calls == 1


async def test_the_mjpeg_frame_does_not_wait_for_a_keyframe(monkeypatch):
    """Every frame of a stream, not a one-off snapshot."""

    async def _fake_still_stream(request, image_cb, content_type, interval):
        await image_cb()
        return "response"

    monkeypatch.setattr(
        "custom_components.dahua.camera.async_get_still_stream", _fake_still_stream
    )
    camera = _camera(True, monkeypatch)

    await camera.handle_async_still_stream(object(), 0.5)

    assert camera.stream_keyframe_waits is False


async def test_an_ordinary_channel_keeps_home_assistants_own_handler(monkeypatch):
    """Nothing changes for a channel the recorder does photograph."""
    called = {}

    async def _super(self, request, interval):
        called["super"] = True
        return "ha-response"

    monkeypatch.setattr(
        "homeassistant.components.camera.Camera.handle_async_still_stream", _super
    )
    camera = _camera(False, monkeypatch)

    assert await camera.handle_async_still_stream(object(), 0.5) == "ha-response"
    assert called == {"super": True}
    assert getattr(camera, "stream_calls", 0) == 0


async def test_the_webrtc_provider_answers_first_when_there_is_one(monkeypatch):
    """The order Home Assistant uses, so the two paths cannot disagree."""
    camera = _camera(True, monkeypatch)
    provider = SimpleNamespace()

    async def async_get_image(cam, width=None, height=None):
        return b"from-webrtc"

    provider.async_get_image = async_get_image
    monkeypatch.setattr(
        type(camera), "webrtc_provider", property(lambda self: provider), raising=False
    )

    assert await camera._async_stream_image() == b"from-webrtc"
    assert getattr(camera, "stream_calls", 0) == 0


async def test_a_channel_with_no_stream_at_all_returns_nothing(monkeypatch):
    """None is what Home Assistant expects from a camera with no image."""
    camera = _camera(True, monkeypatch)
    camera.stream = None
    monkeypatch.setattr(
        type(camera),
        "supported_features",
        property(lambda self: CameraEntityFeature(0)),
        raising=False,
    )

    assert await camera._async_stream_image() is None
