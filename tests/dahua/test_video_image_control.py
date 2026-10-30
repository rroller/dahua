"""Capability and refusal handling for VideoImageControl."""

from types import SimpleNamespace

from custom_components.dahua import coordinator as coordinator_module
from custom_components.dahua.coordinator import DahuaDataUpdateCoordinator


def _coordinator(table=None, *, raises=None):
    async def read():
        if raises is not None:
            raise raises
        return table or {}

    coordinator = object.__new__(DahuaDataUpdateCoordinator)
    coordinator.client = SimpleNamespace(async_get_video_image_control=read)
    coordinator._channel = 0
    coordinator._probe_refusals = {}
    coordinator._video_image_control_fields = frozenset()
    coordinator.data = {}
    return coordinator


async def test_probe_detects_only_reported_image_control_properties():
    coordinator = _coordinator(
        {
            "table.VideoImageControl[0].Flip": "false",
            "table.VideoImageControl[0].Mirror": "true",
            "table.VideoImageControl[1].Rotate90": "0",
        }
    )

    assert await coordinator._async_probe_video_image_control()
    assert coordinator.supports_video_image_control("Flip")
    assert coordinator.supports_video_image_control("Mirror")
    assert not coordinator.supports_video_image_control("Rotate90")


async def test_refused_image_control_read_is_non_fatal():
    coordinator = _coordinator()

    assert not await coordinator._async_probe_video_image_control()
    assert coordinator._video_image_control_fields == frozenset()


async def test_poll_refusal_carries_only_previous_image_control_fields():
    coordinator = _coordinator(raises=TimeoutError("refused"))
    coordinator.data = {
        "table.VideoImageControl[0].Flip": "true",
        "table.VideoImageControl[0].Rotate90": "0",
        "table.VideoImageControl[0].Mirror": "false",
        "table.VideoColor[0][0].Gamma": "50",
    }

    assert await coordinator._async_fetch_video_image_control() == {
        "table.VideoImageControl[0].Flip": "true",
        "table.VideoImageControl[0].Rotate90": "0",
        "table.VideoImageControl[0].Mirror": "false",
    }


def test_image_control_capability_can_be_retried_after_silence():
    assert "video_image_control" in coordinator_module.RETRYABLE_CAPABILITIES
