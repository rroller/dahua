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
    assert (
        client_module.EVENT_STREAM_READ_TIMEOUT_SECONDS
        > client_module.EVENT_STREAM_HEARTBEAT_SECONDS * 2
    )


async def test_stalled_stream_ends_so_the_caller_can_reconnect(
    monkeypatch, socket_enabled
):
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
                client.stream_events(
                    lambda data, channel: received.append(data), ["All"], 0
                ),
                timeout=10,
            )
        # aiohttp's read timeout subclasses asyncio.TimeoutError, so identify
        # it precisely rather than by the base class the outer wait_for uses.
        assert isinstance(
            caught.value, aiohttp.ServerTimeoutError
        ), "expected the socket read timeout, got %r" % (caught.value,)
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

    def __init__(self, status=200, chunks=(), after_chunk=None, response_headers=None):
        self.status = status
        self.chunks = chunks
        self.after_chunk = after_chunk
        self.response_headers = response_headers or {}
        self.requested = False

    async def request(self, method, url, headers=None, **kwargs):
        self.requested = True
        return _EndingResponse(
            self.status, self.chunks, self.after_chunk, self.response_headers
        )


class _EndingResponse:
    def __init__(self, status, chunks, after_chunk=None, response_headers=None):
        self.status = status
        self.headers = response_headers or {}
        self.content = _Content(chunks, after_chunk)

    def raise_for_status(self):
        if self.status >= 400:
            raise aiohttp.ClientResponseError(None, (), status=self.status)

    def close(self):
        return None


class _Content:
    def __init__(self, chunks, after_chunk=None):
        self._chunks = chunks
        self._after_chunk = after_chunk

    async def iter_chunks(self):
        for index, c in enumerate(self._chunks):
            yield c, True
            if self._after_chunk is not None:
                self._after_chunk(index)


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
    client = DahuaClient(
        "u", "p", "d", 80, 554, _EndingSession(chunks=[b"a", b"b", b"c"])
    )

    with pytest.raises(EventStreamClosed):
        await client.stream_events(lambda data, channel: got.append(data), ["All"], 0)

    assert got == [b"a", b"b", b"c"]


async def test_multipart_chunks_are_buffered_until_boundary():
    """Verify that split TCP chunks of a multipart event are buffered and delivered as a complete block."""
    got = []
    # Event split across two chunks, followed by a third chunk with next boundary
    chunks = [
        b'--myboundary\r\nContent-Type: text/plain\r\n\r\nCode=TrafficParking;data={"part": 1,',
        b' "part": 2}\r\n--myboundary\r\nContent-Type: text/plain\r\n\r\nHeartbeat\r\n--myboundary\r\n',
    ]
    client = DahuaClient("u", "p", "d", 80, 554, _EndingSession(chunks=chunks))

    with pytest.raises(EventStreamClosed):
        await client.stream_events(lambda data, channel: got.append(data), ["All"], 0)

    assert len(got) == 2
    assert b'"part": 1, "part": 2' in got[0]
    assert b"Heartbeat" in got[1]


async def test_content_length_delivers_split_part_before_next_boundary():
    """A complete part must not wait for the next heartbeat boundary."""
    got = []
    payload = b"Code=DoorbellPressed;action=Pulse;index=0"
    part = (
        b"--myboundary\r\n"
        b"Content-Type: text/plain\r\n"
        b"Content-Length: " + str(len(payload)).encode() + b"\r\n\r\n" + payload
    )
    chunks = [part[:-12], part[-12:]]

    def after_chunk(index):
        if index == 0:
            assert got == []
        elif index == 1:
            # This assertion runs before the iterator can end. The old #678
            # loop failed here because it waited for boundary N+1.
            assert got == [part]

    client = DahuaClient(
        "u",
        "p",
        "d",
        80,
        554,
        _EndingSession(chunks=chunks, after_chunk=after_chunk),
    )

    with pytest.raises(EventStreamClosed):
        await client.stream_events(lambda data, channel: got.append(data), ["All"], 0)

    assert got == [part]


async def test_declared_boundary_delivers_by_content_length_too():
    """A device-declared multipart boundary must keep the immediate path."""
    got = []
    boundary = b"--abc123"
    payload = b"Code=VideoMotion;action=Start;index=0"
    part = (
        boundary + b"\r\n"
        b"Content-Type: text/plain\r\n"
        b"Content-Length: " + str(len(payload)).encode() + b"\r\n\r\n" + payload
    )

    def after_chunk(index):
        assert index == 0
        assert got == [part]

    client = DahuaClient(
        "u",
        "p",
        "d",
        80,
        554,
        _EndingSession(
            chunks=[part],
            after_chunk=after_chunk,
            response_headers={
                "Content-Type": "multipart/x-mixed-replace; boundary=abc123"
            },
        ),
    )

    with pytest.raises(EventStreamClosed):
        await client.stream_events(lambda data, channel: got.append(data), ["All"], 0)

    assert got == [part]


async def test_oversized_content_length_falls_back_to_next_boundary():
    """A wrong Content-Length must not stall later events behind it."""
    got = []
    first_payload = b'Code=TrafficParking;data={"truncated": true'
    second_payload = b"Code=DoorbellPressed;action=Pulse;index=0"

    first_part = (
        b"--myboundary\r\n"
        b"Content-Type: text/plain\r\n"
        b"Content-Length: "
        + str(len(first_payload) + 100).encode()
        + b"\r\n\r\n"
        + first_payload
        + b"\r\n"
    )
    second_part = (
        b"--myboundary\r\n"
        b"Content-Type: text/plain\r\n"
        b"Content-Length: "
        + str(len(second_payload)).encode()
        + b"\r\n\r\n"
        + second_payload
    )

    client = DahuaClient(
        "u",
        "p",
        "d",
        80,
        554,
        _EndingSession(chunks=[first_part + second_part]),
    )

    with pytest.raises(EventStreamClosed):
        await client.stream_events(lambda data, channel: got.append(data), ["All"], 0)

    assert len(got) == 2
    assert first_payload in got[0]
    assert second_payload in got[1]


# --- a device with no CGI event path at all ----------------------------------
#
# An SL300 answers 404 to every /cgi-bin/ path it has, eventManager.cgi included, so
# the multipart stream this integration normally attaches to does not exist on it. The
# subscription is not failed: it is made over RPC2 instead, which is the only transport
# such a device has. Without this its binary sensors could never move, whatever the
# vendor app showed.
#
# The fallback is the one place these two transports meet, and it was uncovered.


class _RecordingSession(_EndingSession):
    """Remembers the URL asked for, and whether the response was closed."""

    def __init__(self, status=200, chunks=()):
        super().__init__(status=status, chunks=chunks)
        self.urls = []
        self.closed = 0

    async def request(self, method, url, headers=None, **kwargs):
        self.urls.append(url)
        self.requested = True
        session = self

        class _Recording(_EndingResponse):
            def close(self):
                session.closed += 1

        return _Recording(self.status, self.chunks)


def _polled(monkeypatch):
    """Record the RPC2 poller being used in place of the CGI stream."""
    calls = []

    async def _poll(self, on_receive, events, channel):
        calls.append((list(events), channel))

    monkeypatch.setattr(DahuaClient, "_stream_events_rpc2", _poll)
    return calls


async def test_a_404_subscribes_over_rpc2_instead_of_failing(monkeypatch):
    calls = _polled(monkeypatch)
    client = DahuaClient("u", "p", "d", 80, 554, _RecordingSession(status=404))

    await client.stream_events(lambda data, channel: None, ["VideoMotion"], 0)

    assert calls == [
        (["VideoMotion"], 0)
    ], "the subscription was not made over RPC2: %s" % (calls,)


async def test_a_501_does_the_same(monkeypatch):
    """The other status in EVENT_CGI_ABSENT. Asserted rather than assumed, because a
    device answering 501 is as unable to serve the stream as one answering 404."""
    calls = _polled(monkeypatch)
    client = DahuaClient("u", "p", "d", 80, 554, _RecordingSession(status=501))

    await client.stream_events(lambda data, channel: None, ["VideoMotion"], 0)

    assert calls, "a 501 was treated as a real error rather than an absent endpoint"


async def test_the_refused_response_is_closed_before_falling_back(monkeypatch):
    """Closed explicitly rather than left to the finally, because returning through
    the finally would skip the poller entirely. So the close and the fallback both
    have to happen, and this is what says the connection is not leaked for the life
    of the poll."""
    _polled(monkeypatch)
    session = _RecordingSession(status=404)
    client = DahuaClient("u", "p", "d", 80, 554, session)

    await client.stream_events(lambda data, channel: None, ["VideoMotion"], 0)

    assert session.closed >= 1, "the refused response was never closed"


async def test_a_real_http_error_does_not_fall_back(monkeypatch):
    """The gate has to still close. A 401 means the credentials are wrong, and quietly
    moving to another transport would hide that behind a device that simply reports
    nothing."""
    calls = _polled(monkeypatch)
    client = DahuaClient("u", "p", "d", 80, 554, _RecordingSession(status=401))

    with pytest.raises(aiohttp.ClientResponseError):
        await client.stream_events(lambda data, channel: None, ["VideoMotion"], 0)

    assert calls == [], "a 401 was answered by changing transport"


async def test_every_selected_code_reaches_the_poller(monkeypatch):
    """The fallback passes the caller's selection through. Dropping to All here would
    subscribe to everything on a device the user had narrowed deliberately."""
    calls = _polled(monkeypatch)
    client = DahuaClient("u", "p", "d", 80, 554, _RecordingSession(status=404))

    await client.stream_events(
        lambda data, channel: None, ["VideoMotion", "CrossLineDetection"], 3
    )

    assert calls == [(["VideoMotion", "CrossLineDetection"], 3)]


async def test_several_codes_are_asked_for_as_one_comma_separated_list():
    """The CGI subscription takes them in one bracketed list, so this is what the
    device is actually asked before any of the above matters."""
    session = _RecordingSession(chunks=[])
    client = DahuaClient("u", "p", "d", 80, 554, session)

    with pytest.raises(EventStreamClosed):
        await client.stream_events(
            lambda data, channel: None, ["VideoMotion", "AlarmLocal"], 0
        )

    assert "codes=[VideoMotion,AlarmLocal]" in session.urls[0], session.urls


async def test_all_is_sent_as_the_word_rather_than_a_list():
    session = _RecordingSession(chunks=[])
    client = DahuaClient("u", "p", "d", 80, 554, session)

    with pytest.raises(EventStreamClosed):
        await client.stream_events(lambda data, channel: None, ["All"], 0)

    assert "codes=[All]" in session.urls[0], session.urls


async def test_a_boundary_split_across_two_chunks_is_still_assembled():
    """#995's second half.

    The loop used to ask whether this chunk or the buffer contained the
    delimiter. A boundary split across two TCP chunks is in neither, so both
    halves went straight to the handler and the part was never assembled --
    the handler saw fragments of an event instead of the event.

    The device declares the boundary in its Content-Type here, which is the
    signal the loop now uses.
    """
    got = []
    payload = b"Code=CrossLineDetection;action=Start;index=0"
    part = (
        b"--myboundary\r\n"
        b"Content-Type: text/plain\r\n"
        b"Content-Length: " + str(len(payload)).encode() + b"\r\n\r\n" + payload
    )
    # The split falls inside the opening delimiter itself.
    chunks = [part[:6], part[6:]]
    client = DahuaClient(
        "u",
        "p",
        "d",
        80,
        554,
        _EndingSession(
            chunks=chunks,
            response_headers={
                "Content-Type": "multipart/x-mixed-replace; boundary=myboundary"
            },
        ),
    )

    with pytest.raises(EventStreamClosed):
        await client.stream_events(lambda data, channel: got.append(data), ["All"], 0)

    assert got == [part], "the split boundary was not reassembled: %s" % got


async def test_a_heartbeat_with_no_blank_line_does_not_truncate_the_next_event():
    """#995's first half, through the stream rather than the parser alone.

    Measured on a DH-SD42A212TN-HNI (firmware 2.640.0000000.2.R): the
    heartbeat part has no blank line between its headers and its payload. The
    header search ran past the next boundary, found the separator in the
    event's headers, and applied the heartbeat's `Content-Length: 9` to the
    event -- delivering nine bytes of it, `Code=Cros`, with no action and no
    index, so no sensor moved.
    """
    got = []
    heartbeat = (
        b"--myboundary\r\n"
        b"Content-Type: text/plain\r\n"
        b"Content-Length:9\r\n"
        b"Heartbeat\r\n"
    )
    payload = b"Code=CrossLineDetection;action=Start;index=0"
    event = (
        b"--myboundary\r\n"
        b"Content-Type: text/plain\r\n"
        b"Content-Length: " + str(len(payload)).encode() + b"\r\n\r\n" + payload
    )
    client = DahuaClient(
        "u", "p", "d", 80, 554, _EndingSession(chunks=[heartbeat + event])
    )

    with pytest.raises(EventStreamClosed):
        await client.stream_events(lambda data, channel: got.append(data), ["All"], 0)

    delivered = b"".join(got)
    assert b"Code=CrossLineDetection;action=Start;index=0" in delivered, (
        "the event was truncated to the heartbeat's length: %s" % got
    )
    assert b"Code=Cros;" not in delivered and not any(
        part.endswith(b"Code=Cros") for part in got
    ), ("the heartbeat's Content-Length was applied to the event: %s" % got)
