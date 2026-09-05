"""The camera serves RTSP through the stream component, so HLS, not WebRTC."""

from homeassistant.components.camera import CameraEntityFeature, StreamType

from custom_components.dahua.camera import DahuaCamera


def _camera():
    """A camera with only the plumbing camera_capabilities reads.

    Home Assistant sets these in Camera.__init__, which needs the full entity
    machinery; this asks the real capability logic the same question it is
    asked at runtime.
    """
    c = object.__new__(DahuaCamera)
    c._attr_supported_features = CameraEntityFeature.STREAM
    c._supports_native_async_webrtc = False
    c._webrtc_provider = None
    c._cache = {}
    return c


def test_the_camera_advertises_hls():
    assert StreamType.HLS in _camera().camera_capabilities.frontend_stream_types


def test_the_camera_does_not_claim_webrtc_it_cannot_serve():
    """There is no async_handle_async_webrtc_offer here. Advertising WebRTC
    would leave the frontend negotiating with something that never answers.

    camera.py used to set _attr_frontend_stream_type = StreamType.WEB_RTC.
    Home Assistant derives capabilities from _supports_native_async_webrtc and
    _webrtc_provider instead and never reads that attribute, so the line did
    nothing -- but it said the opposite of what this integration does.
    """
    caps = _camera().camera_capabilities

    assert StreamType.WEB_RTC not in caps.frontend_stream_types


def test_the_module_no_longer_mentions_the_unused_import():
    import inspect

    from custom_components.dahua import camera as camera_module

    assert "StreamType" not in inspect.getsource(camera_module)
