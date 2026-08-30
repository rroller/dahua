"""Tests for the event stream's timeout behaviour."""
import asyncio

import aiohttp
from aiohttp import web
import pytest

from custom_components.dahua import client as client_module
from custom_components.dahua.client import DahuaClient


class CapturingSession:
    """Records the kwargs a request was issued with."""

    def __init__(self):
        self.kwargs = None

    async def request(self, method, url, headers=None, **kwargs):
        self.kwargs = kwargs
        raise RuntimeError("stop here, we only want the arguments")


async def test_stream_request_is_bounded_on_read_not_total():
    """The long poll must not inherit the session's default total timeout."""
    session = CapturingSession()
    client = DahuaClient("u", "p", "d", 80, 554, session)

    # stream_events swallows its own exceptions, so this returns normally.
    await client.stream_events(lambda data, channel: None, ["All"], 0)

    timeout = session.kwargs["timeout"]
    assert isinstance(timeout, aiohttp.ClientTimeout)
    # A total timeout would tear down a healthy stream on a fixed cycle.
    assert timeout.total is None
    assert timeout.sock_read == client_module.EVENT_STREAM_READ_TIMEOUT_SECONDS


async def test_heartbeat_interval_matches_the_read_timeout():
    """The read timeout is only meaningful if it exceeds the heartbeat."""
    assert (client_module.EVENT_STREAM_READ_TIMEOUT_SECONDS
            > client_module.EVENT_STREAM_HEARTBEAT_SECONDS * 2)


async def test_stalled_stream_ends_so_the_caller_can_reconnect(monkeypatch):
    """A socket that goes quiet must not hold the stream open forever."""
    monkeypatch.setattr(client_module, "EVENT_STREAM_READ_TIMEOUT_SECONDS", 1)

    async def handler(request):
        response = web.StreamResponse(status=200)
        await response.prepare(request)
        await response.write(b"Heartbeat")
        await asyncio.sleep(5)  # connection stays open, nothing more arrives
        return response

    app = web.Application()
    app.router.add_get("/cgi-bin/eventManager.cgi", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = list(runner.addresses)[0][1]

    session = aiohttp.ClientSession()
    client = DahuaClient("u", "p", "127.0.0.1", port, 554, session)
    received = []

    try:
        await asyncio.wait_for(
            client.stream_events(lambda data, channel: received.append(data), ["All"], 0),
            timeout=10,
        )
    except asyncio.TimeoutError:
        pytest.fail("stream did not end after the socket went quiet")
    finally:
        await session.close()
        await runner.cleanup()

    assert received, "the heartbeat before the stall should have been delivered"
