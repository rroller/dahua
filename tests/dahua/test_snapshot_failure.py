"""A camera that cannot produce a snapshot right now has not failed.

#290, open since July 2023 and reported twice. `camera.snapshot` was returning
a service error whenever the device refused a still:

    Error executing service: <ServiceCall camera.snapshot ...>

@parautenbach found the line and said so on the issue: `async_camera_image`
passed the call straight through, so anything the device did came out of the
service call.

These devices refuse under load, and a recorder refuses more often because
every channel is competing for the same box. That is an ordinary transient
condition, not a fault in the automation asking for the picture.

Home Assistant expects None from a camera that cannot produce an image right
now. The previous frame stays, and nothing raises.

Only transport failures are caught, so a bug in the snapshot path still comes
out rather than being quietly turned into a blank frame.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import ClientError, ClientResponseError

from custom_components.dahua.camera import DahuaCamera


IMAGE = b"\xff\xd8\xff\xe0 a jpeg \xff\xd9"


def _camera(snapshot):
    """A camera whose client does whatever `snapshot` does."""
    cam = object.__new__(DahuaCamera)
    cam._channel_number = 1
    cam._name = "Front Door"
    cam._coordinator = SimpleNamespace(
        client=SimpleNamespace(async_get_snapshot=snapshot))
    return cam


async def test_a_snapshot_is_returned_when_the_camera_answers():
    cam = _camera(AsyncMock(return_value=IMAGE))

    assert await cam.async_camera_image() == IMAGE


async def test_a_refused_snapshot_is_not_an_error():
    """The #290 case. A recorder under load refuses, and that is all it is."""
    cam = _camera(AsyncMock(side_effect=ClientError("connection reset")))

    assert await cam.async_camera_image() is None


async def test_a_bad_status_is_not_an_error():
    """ClientResponseError is what a 500 from a busy device arrives as."""
    refusal = ClientResponseError(request_info=None, history=(), status=500)
    cam = _camera(AsyncMock(side_effect=refusal))

    assert await cam.async_camera_image() is None


@pytest.mark.parametrize("timeout", [TimeoutError(), asyncio.TimeoutError()])
async def test_a_slow_camera_is_not_an_error(timeout):
    cam = _camera(AsyncMock(side_effect=timeout))

    assert await cam.async_camera_image() is None


async def test_a_bug_in_here_still_comes_out():
    """Catching everything would turn our own mistakes into blank frames."""
    cam = _camera(AsyncMock(side_effect=ValueError("someone broke the parser")))

    with pytest.raises(ValueError):
        await cam.async_camera_image()


async def test_the_width_and_height_arguments_are_accepted():
    """Home Assistant passes them; the signature has to keep taking them."""
    cam = _camera(AsyncMock(return_value=IMAGE))

    assert await cam.async_camera_image(width=640, height=480) == IMAGE
