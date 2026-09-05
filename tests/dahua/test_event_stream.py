"""Tests for the event stream's timeout behaviour."""
import asyncio

import aiohttp
from aiohttp import web
import pytest

from custom_components.dahua import client as client_module
from custom_components.dahua.client import DahuaClient, EventStreamClosed


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

    # The session raises to stop the call once it has the arguments, and
    # stream_events no longer swallows that.
    with pytest.raises(RuntimeError):
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


async def test_stalled_stream_ends_so_the_caller_can_reconnect(monkeypatch, socket_enabled):
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
        with pytest.raises(Exception) as caught:
            await asyncio.wait_for(
                client.stream_events(lambda data, channel: received.append(data), ["All"], 0),
                timeout=10,
            )
        # aiohttp's read timeout subclasses asyncio.TimeoutError, so identify
        # it precisely rather than by the base class the outer wait_for uses.
        assert isinstance(caught.value, aiohttp.ServerTimeoutError),             "expected the socket read timeout, got %r" % (caught.value,)
    finally:
        await session.close()
        await runner.cleanup()

    assert received, "the heartbeat before the stall should have been delivered"


# --- a stream that ends must say so ----------------------------------------
#
# Every failure used to be caught inside stream_events and logged at DEBUG, so
# the caller's warning was unreachable and a device refusing the subscription
# left no trace anywhere. That silence is why an affected user's Home Assistant
# log contained nothing at all about their NVR.

class _EndingSession:
    """A device that accepts the subscription and then closes it."""

    def __init__(self, status=200, chunks=()):
        self.status = status
        self.chunks = chunks
        self.requested = False

    async def request(self, method, url, headers=None, **kwargs):
        self.requested = True
        return _EndingResponse(self.status, self.chunks)


class _EndingResponse:
    def __init__(self, status, chunks):
        self.status = status
        self.headers = {}
        self.content = _Content(chunks)

    def raise_for_status(self):
        if self.status >= 400:
            raise aiohttp.ClientResponseError(None, (), status=self.status)

    def close(self):
        return None


class _Content:
    def __init__(self, chunks):
        self._chunks = chunks

    async def iter_chunks(self):
        for c in self._chunks:
            yield c, True


async def test_a_device_that_closes_the_stream_raises():
    """A long poll that returns is a failure, not a result."""
    client = DahuaClient("u", "p", "d", 80, 554, _EndingSession(chunks=[b"Heartbeat"]))

    with pytest.raises(EventStreamClosed):
        await client.stream_events(lambda data, channel: None, ["All"], 0)


async def test_an_empty_200_raises_too():
    """The quietest failure of all: attach accepted, nothing ever sent."""
    client = DahuaClient("u", "p", "d", 80, 554, _EndingSession(chunks=[]))

    with pytest.raises(EventStreamClosed):
        await client.stream_events(lambda data, channel: None, ["All"], 0)


async def test_an_http_error_reaches_the_caller():
    client = DahuaClient("u", "p", "d", 80, 554, _EndingSession(status=401))

    with pytest.raises(aiohttp.ClientResponseError):
        await client.stream_events(lambda data, channel: None, ["All"], 0)


async def test_missing_credentials_raise_instead_of_looping_silently():
    """Name the reason: without it this passes on the end-of-stream raise
    instead, and the guard could be deleted with every test still green."""
    session = _EndingSession()
    client = DahuaClient(None, None, "d", 80, 554, session)

    with pytest.raises(EventStreamClosed) as caught:
        await client.stream_events(lambda data, channel: None, ["All"], 0)

    assert "credentials" in str(caught.value)
    assert session.requested is False, "it tried to talk to the device anyway"


async def test_chunks_still_reach_the_handler_before_the_close():
    got = []
    client = DahuaClient("u", "p", "d", 80, 554,
                         _EndingSession(chunks=[b"a", b"b", b"c"]))

    with pytest.raises(EventStreamClosed):
        await client.stream_events(lambda data, channel: got.append(data), ["All"], 0)

    assert got == [b"a", b"b", b"c"]
