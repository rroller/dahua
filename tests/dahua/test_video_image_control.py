"""Capability and refusal handling for VideoImageControl."""

from types import SimpleNamespace

from aiohttp import ClientResponseError

from custom_components.dahua import coordinator as coordinator_module
from custom_components.dahua.client import DahuaClient
from custom_components.dahua.coordinator import DahuaDataUpdateCoordinator


def _coordinator(table=None, *, channel=0, raises=None):
    async def read():
        if raises is not None:
            raise raises
        return table or {}

    coordinator = object.__new__(DahuaDataUpdateCoordinator)
    coordinator.client = SimpleNamespace(async_get_video_image_control=read)
    coordinator._channel = channel
    coordinator.get_channel = lambda: coordinator._channel
    coordinator._probe_refusals = {}
    coordinator._video_image_control_fields = frozenset()
    coordinator.data = {}
    return coordinator


async def test_probe_detects_only_reported_image_control_properties():
    coordinator = _coordinator(
        {
            "table.VideoImageControl[0].Flip": "false",
            "table.VideoImageControl[0].Mirror": "false",
            "table.VideoImageControl[1].Rotate90": "0",
            "table.VideoImageControl[3].Flip": "true",
            "table.VideoImageControl[3].Mirror": "true",
            "table.VideoImageControl[3].Rotate90": "0",
        },
        channel=3,
    )

    assert await coordinator._async_probe_video_image_control()
    assert coordinator.supports_video_image_control("Flip")
    assert coordinator.supports_video_image_control("Mirror")
    assert coordinator.supports_video_image_control("Rotate90")


async def test_probe_and_state_use_only_the_coordinators_channel():
    coordinator = _coordinator(
        {
            "table.VideoImageControl[0].Mirror": "false",
            "table.VideoImageControl[3].Mirror": "true",
        },
        channel=3,
    )

    assert await coordinator._async_probe_video_image_control()
    assert coordinator.supports_video_image_control("Mirror")

    coordinator.data = {
        "table.VideoImageControl[0].Mirror": "false",
        "table.VideoImageControl[3].Mirror": "true",
    }
    assert coordinator.get_video_image_control("Mirror") is True


async def test_missing_channel_row_does_not_inherit_other_channel_capabilities_or_state():
    coordinator = _coordinator(
        {
            "table.VideoImageControl[0].Mirror": "true",
            "table.VideoImageControl[0].Flip": "true",
            "table.VideoImageControl[0].Rotate90": "0",
        },
        channel=3,
    )

    assert not await coordinator._async_probe_video_image_control()
    assert not coordinator.supports_video_image_control("Mirror")
    assert not coordinator.supports_video_image_control("Flip")
    assert not coordinator.supports_video_image_control("Rotate90")

    coordinator.data = {
        "table.VideoImageControl[0].Mirror": "true",
        "table.VideoImageControl[0].Flip": "true",
        "table.VideoImageControl[0].Rotate90": "0",
    }
    assert coordinator.get_video_image_control("Mirror") is None
    assert coordinator.get_video_image_control("Flip") is None
    assert coordinator.get_video_image_control("Rotate90") is None


async def test_channel_zero_probe_and_state_still_use_row_zero():
    coordinator = _coordinator(
        {
            "table.VideoImageControl[0].Mirror": "true",
            "table.VideoImageControl[3].Mirror": "false",
        }
    )

    assert await coordinator._async_probe_video_image_control()
    assert coordinator.supports_video_image_control("Mirror")
    coordinator.data = {
        "table.VideoImageControl[0].Mirror": "true",
        "table.VideoImageControl[3].Mirror": "false",
    }
    assert coordinator.get_video_image_control("Mirror") is True


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


async def test_http_403_poll_refusal_preserves_image_controls():
    client = object.__new__(DahuaClient)
    requested_urls = []

    async def read(url):
        requested_urls.append(url)
        if len(requested_urls) == 1:
            raise ClientResponseError(
                request_info=SimpleNamespace(
                    real_url="http://camera/cgi-bin/configManager.cgi"
                ),
                history=(),
                status=403,
            )
        return {}

    client.get = read
    coordinator = _coordinator()
    coordinator.client = client
    coordinator.data = {
        "table.VideoImageControl[0].Mirror": "true",
        "table.VideoImageControl[0].Flip": "false",
        "table.VideoImageControl[0].Rotate90": "1",
        "table.VideoColor[0][0].Gamma": "50",
    }

    carried = await coordinator._async_fetch_video_image_control()

    assert carried == {
        "table.VideoImageControl[0].Mirror": "true",
        "table.VideoImageControl[0].Flip": "false",
        "table.VideoImageControl[0].Rotate90": "1",
    }
    assert coordinator.data == {
        "table.VideoImageControl[0].Mirror": "true",
        "table.VideoImageControl[0].Flip": "false",
        "table.VideoImageControl[0].Rotate90": "1",
        "table.VideoColor[0][0].Gamma": "50",
    }
    assert await coordinator._async_fetch_video_image_control() == {}
    assert requested_urls == [
        "/cgi-bin/configManager.cgi?action=getConfig&name=VideoImageControl",
        "/cgi-bin/configManager.cgi?action=getConfig&name=VideoImageControl",
    ]


def test_image_control_capability_can_be_retried_after_silence():
    assert "video_image_control" in coordinator_module.RETRYABLE_CAPABILITIES
