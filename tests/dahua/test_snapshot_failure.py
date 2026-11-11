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
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import ClientError, ClientResponseError

from custom_components.dahua.camera import DahuaCamera
from custom_components.dahua import client as client_module
from custom_components.dahua.client import (
    MAX_CONCURRENT_REQUESTS_PER_HOST,
    REQUEST_REACHED_DEVICE,
    DahuaClient,
)

IMAGE = b"\xff\xd8\xff\xe0 a jpeg \xff\xd9"


def _camera(snapshot):
    """A camera whose client does whatever `snapshot` does."""
    cam = object.__new__(DahuaCamera)
    cam._channel_number = 1
    cam._name = "Front Door"
    cam._coordinator = SimpleNamespace(
        client=SimpleNamespace(async_get_snapshot=snapshot)
    )
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


# --- a still nobody waited for -----------------------------------------------
#
# #1197. Home Assistant gives a camera CAMERA_IMAGE_TIMEOUT, which is 10
# seconds, and then cancels. This integration allows MAX_QUEUE_WAIT_SECONDS
# (60) for a request slot and TIMEOUT_SECONDS (20) for the device after that,
# so the caller gives up first and the still arrives here as a CancelledError.
# That is a BaseException, so none of the handlers above see it: nothing was
# logged, and the reporter saw an HTTP 500 with an empty log.
#
# The two ways it happens need opposite fixes and look identical from outside,
# so what is reported is which of them it was.


async def _cancel_after(cam, delay=0.05):
    """Drive async_camera_image the way Home Assistant's proxy does."""
    try:
        async with asyncio.timeout(delay):
            await cam.async_camera_image()
    except (asyncio.CancelledError, TimeoutError) as error:
        return error
    return None


async def test_a_still_abandoned_in_the_queue_says_so(caplog):
    """Never sent. The request was still waiting for one of the host's slots,
    so the message must not blame the device, which was never asked."""

    async def never_gets_a_slot(_channel):
        await asyncio.sleep(3600)

    caplog.set_level(logging.DEBUG, logger="custom_components.dahua")
    await _cancel_after(_camera(never_gets_a_slot))

    assert "without ever being sent" in caplog.text
    assert "waiting for one of this host's 2 request slots" in caplog.text
    assert "reached the device" not in caplog.text


async def test_a_still_abandoned_at_the_device_says_so(caplog):
    """Past the queue. `get_bytes` records that, and the message changes."""

    async def reaches_the_device_then_stalls(_channel):
        REQUEST_REACHED_DEVICE.set(True)
        await asyncio.sleep(3600)

    caplog.set_level(logging.DEBUG, logger="custom_components.dahua")
    await _cancel_after(_camera(reaches_the_device_then_stalls))

    assert "had reached the device and the device had not answered" in caplog.text
    assert "without ever being sent" not in caplog.text


async def test_the_cancellation_is_not_swallowed(caplog):
    """It must still come out. Returning None here would hand Home Assistant a
    blank frame for a request it had already given up on, and swallowing a
    CancelledError breaks asyncio.timeout's accounting."""

    async def never_answers(_channel):
        await asyncio.sleep(3600)

    caplog.set_level(logging.DEBUG, logger="custom_components.dahua")
    raised = await _cancel_after(_camera(never_answers))

    assert raised is not None, "the caller was told nothing went wrong"
    assert isinstance(raised, (asyncio.CancelledError, TimeoutError))


async def test_a_stale_reading_does_not_describe_the_next_still(caplog):
    """One task serves many stills. A request that reached the device must not
    leave the flag set for a later one that never got a slot."""

    async def reaches_the_device(_channel):
        REQUEST_REACHED_DEVICE.set(True)
        return IMAGE

    cam = _camera(reaches_the_device)
    assert await cam.async_camera_image() == IMAGE

    async def never_gets_a_slot(_channel):
        await asyncio.sleep(3600)

    cam._coordinator.client.async_get_snapshot = never_gets_a_slot
    caplog.set_level(logging.DEBUG, logger="custom_components.dahua")
    await _cancel_after(cam)

    assert "without ever being sent" in caplog.text
    assert "reached the device" not in caplog.text


async def test_nothing_is_said_when_the_still_arrives(caplog):
    """The quiet path stays quiet. A line per successful still would be noise
    on every dashboard refresh."""
    caplog.set_level(logging.DEBUG, logger="custom_components.dahua")
    cam = _camera(AsyncMock(return_value=IMAGE))

    assert await cam.async_camera_image() == IMAGE
    assert "given up on" not in caplog.text


async def test_an_ordinary_refusal_is_still_the_old_message(caplog):
    """A device that refuses has not been abandoned, and the #290 wording is
    what the issue history refers to."""
    caplog.set_level(logging.DEBUG, logger="custom_components.dahua")
    cam = _camera(AsyncMock(side_effect=ClientError("connection reset")))

    assert await cam.async_camera_image() is None
    assert "could not fetch a snapshot" in caplog.text
    assert "given up on" not in caplog.text


# --- the flag is set by the real client, not only by the doubles above -------
#
# The tests above hand `async_get_snapshot` a double that sets
# REQUEST_REACHED_DEVICE itself, so none of them reaches the line in
# `get_bytes` that production depends on. Breaking that line reddened nothing
# until these two existed.


class _OneResponse:
    status = 200
    headers = {}

    def raise_for_status(self):
        return None

    async def read(self):
        return IMAGE

    def close(self):
        return None


class _SlowSession:
    """Stands in for aiohttp, holding each request open for `hold` seconds."""

    def __init__(self, hold=0.0):
        self.hold = hold
        self.calls = 0

    async def request(self, method, url, headers=None, **kwargs):
        self.calls += 1
        if self.hold:
            await asyncio.sleep(self.hold)
        return _OneResponse()


@pytest.fixture(autouse=True)
def _clean_limiters():
    """The per-host limiter registry is module state."""
    client_module._HOST_LIMITS.clear()
    yield
    client_module._HOST_LIMITS.clear()


async def test_get_bytes_records_that_the_request_reached_the_device():
    """The real line. A fetch that got a slot must leave the flag set, or the
    message above would blame the queue for every slow device."""
    REQUEST_REACHED_DEVICE.set(False)
    client = DahuaClient("u", "p", "10.90.0.1", 80, 554, _SlowSession())

    assert await client.get_bytes("/cgi-bin/snapshot.cgi?channel=1") == IMAGE
    assert REQUEST_REACHED_DEVICE.get() is True


async def test_a_request_still_in_the_queue_has_not_reached_the_device():
    """The neighbouring case, and the one #1197 is about. With both slots held,
    a third request is cancelled before it is ever sent, and must not claim the
    device was asked."""
    REQUEST_REACHED_DEVICE.set(False)
    session = _SlowSession(hold=3600)
    client = DahuaClient("u", "p", "10.90.0.2", 80, 554, session)

    holders = [
        asyncio.ensure_future(client.get_bytes("/cgi-bin/snapshot.cgi?channel=1"))
        for _ in range(MAX_CONCURRENT_REQUESTS_PER_HOST)
    ]
    await asyncio.sleep(0.05)
    assert session.calls == MAX_CONCURRENT_REQUESTS_PER_HOST, "the slots are not held"

    queued = asyncio.ensure_future(client.get_bytes("/cgi-bin/snapshot.cgi?channel=2"))
    await asyncio.sleep(0.05)
    assert not queued.done(), "the third request was not queued"
    assert session.calls == MAX_CONCURRENT_REQUESTS_PER_HOST, "it was sent anyway"

    queued.cancel()
    for task in holders:
        task.cancel()
    await asyncio.gather(queued, *holders, return_exceptions=True)

    assert REQUEST_REACHED_DEVICE.get() is False
