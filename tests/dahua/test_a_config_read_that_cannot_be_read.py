"""#1176: a camera whose CGI answers cannot be read gets its CONFIG over RPC2.

#1183 gave this camera its identity back. @defre92 then measured 3.0.0-rc.5 on
the device and reported the half that was left: with the per-entry RPC2 option
**off**, every config read still goes over CGI, still comes back unreadable and
is still swallowed into `{}`. The camera gets 19 entities instead of 49, and
`switch.motion_detection` reads `off` while motion detection is enabled on the
camera -- a wrong state, not a missing one.

The route already exists. A device whose `configManager.cgi` is **absent**
(an SL300, 404) is remembered in `_HOST_CGI_CONFIG_ABSENT` and read over RPC2
from then on without the option being touched. An answer that cannot be read is
the same decision for a different reason, so it joins that memo rather than
growing a second one.

**Why these tests serve bytes instead of raising a built exception.** The
fallback was reached from an `except` around `response.raise_for_status()`. This
refusal never arrives there. aiohttp synthesises it inside its parser, so it
comes out of the request itself, and a test that hands the client a prepared
`ClientResponseError` cannot tell the two apart -- it would have passed just as
well against the version where nothing happened. Measured over loopback on
aiohttp 3.14.3, across five ways the camera's bytes can be split across reads:

    bytes arrive as                         raises from         accepted
    one write, stray header (#1183's shape) session.get()       no
    one write, the whole phantom response   session.get()       no
    headers and body, then the phantom      -                   yes, body intact
    through the phantom, then its body      session.get()       no
    headers, then body and phantom          -                   yes, body intact

`response.text()` raises in none of them, and the two that are accepted are the
splits where the trailing bytes reach the parser after the message completed.
The camera sends it in one segment -- @defre92's trace shows 486 bytes in a
single `Recv data` block -- so in the field it is the failing shape.
"""

import asyncio
from contextlib import asynccontextmanager

import aiohttp
import pytest

from custom_components.dahua import client as client_module
from custom_components.dahua.client import DahuaClient

# ---------------------------------------------------------------------------
# The bytes
# ---------------------------------------------------------------------------

# @defre92's `curl --trace-ascii` of an authenticated call, as far as it is
# quoted on the issue: a complete, correct 200 and then a whole second HTTP
# response the camera sends although only one request was made. The headers are
# his; the phantom's 21 byte body is not quoted anywhere, so it is filler of the
# length he measured and nothing is claimed about its content.
_HEADERS_200 = (
    b"HTTP/1.1 200 OK\r\n"
    b"Cache-Control: no-store\r\n"
    b"Connection: close\r\n"
    b"Content-Length: 25\r\n"
    b"Content-Type: text/plain;charset=utf-8\r\n"
    b"X-Content-Type-Options: nosniff\r\n"
    b"X-Frame-Options: SAMEORIGIN\r\n"
    b"\r\n"
)
_BODY_200 = b"table.MotionDetect.Enable"
_PHANTOM_400 = (
    b"HTTP/1.1 400 Bad Request\r\n"
    b"Cache-Control: no-store\r\n"
    b"Connection: close\r\n"
    b"Content-Length: 21\r\n"
    b"Content-Type: text/plain;charset=utf-8\r\n"
    b"X-Content-Type-Options: nosniff\r\n"
    b"\r\n"
    b"....21 bytes of body"
)

UNREADABLE = _HEADERS_200 + _BODY_200 + _PHANTOM_400

# What a device sends when it means it: correctly framed, so aiohttp hands back
# a response and `raise_for_status` is what refuses. A recorder answers this to
# snapshot.cgi for a channel it does not have, which is why the status alone
# cannot be the test.
A_REAL_400 = (
    b"HTTP/1.1 400 Bad Request\r\n"
    b"Content-Length: 5\r\n"
    b"Content-Type: text/plain\r\n"
    b"\r\n"
    b"Error"
)

# The absent endpoint this route was built for. Still has to work.
A_REAL_404 = (
    b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nContent-Type: text/plain\r\n\r\n"
)

A_CLEAN_200 = (
    b"HTTP/1.1 200 OK\r\n"
    b"Content-Length: 35\r\n"
    b"Content-Type: text/plain\r\n"
    b"\r\n"
    b"table.MotionDetect[0].Enable=true\r\n"
)

MOTION_DETECT = "/cgi-bin/configManager.cgi?action=getConfig&name=MotionDetect"
VIDEO_IN_MODE = "/cgi-bin/configManager.cgi?action=getConfig&name=VideoInMode"
MACHINE_NAME = "/cgi-bin/magicBox.cgi?action=getMachineName"

# What RPC2 answers instead. Deliberately a value CGI could not have produced in
# these tests, so a passing assertion cannot be the CGI answer leaking through.
RPC2_TABLE = {"table.MotionDetect[0].Enable": "answered-over-rpc2"}


# ---------------------------------------------------------------------------
# A loopback server, because the thing under test is a parser failure
# ---------------------------------------------------------------------------


class _Server:
    """Answers each request with the next scripted payload, and counts them.

    A request past the end of the script is recorded and the connection closed
    without an answer, so a test can assert that no second round trip was made
    and see a disconnect rather than a timeout if one was.

    An asyncio server rather than a thread on purpose. The first version served
    from a daemon thread blocked in `accept`, which `close` on the listening
    socket does not wake, so every test passed and then failed in teardown on
    the suite's lingering-thread check -- and cost two seconds joining a thread
    that was never going to exit.
    """

    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.requests = []
        self.port = None
        self._server = None

    async def start(self):
        self._server = await asyncio.start_server(
            self._handle, "127.0.0.1", 0, reuse_address=True
        )
        self.port = self._server.sockets[0].getsockname()[1]

    async def _handle(self, reader, writer):
        try:
            try:
                self.requests.append(await reader.readuntil(b"\r\n\r\n"))
            except (asyncio.IncompleteReadError, ConnectionError) as partial:
                self.requests.append(getattr(partial, "partial", b""))
            if self.payloads:
                writer.write(self.payloads.pop(0))
                await writer.drain()
        except ConnectionError:
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass

    async def stop(self):
        self._server.close()
        await self._server.wait_closed()


@asynccontextmanager
async def _serving(*payloads):
    server = _Server(payloads)
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


class _Rpc2Reads:
    """Stands in for `_rpc2_get_config`, recording every table it was asked for."""

    def __init__(self, answer=None, raises=None):
        self.answer = RPC2_TABLE if answer is None else answer
        self.raises = raises
        self.asked = []

    async def __call__(self, name):
        self.asked.append(name)
        if self.raises is not None:
            raise self.raises
        return self.answer


async def _read(server, url=MOTION_DETECT, rpc2=None, **kwargs):
    """Drive the real `_request` against the loopback server.

    `_request` rather than `get`, because `get` shares and caches reads per
    device and two reads in one test would be answered once. This is the unit
    the docstring on `_request` names: one caller per shared read reaches here.

    Returns `(answer_or_exception, rpc2)`.
    """
    if rpc2 is None:
        rpc2 = _Rpc2Reads()
    async with aiohttp.ClientSession() as session:
        client = DahuaClient("u", "p", "127.0.0.1", server.port, 554, session)
        client._rpc2_get_config = rpc2
        try:
            return await client._request(url, **kwargs), rpc2
        except Exception as exception:  # pylint: disable=broad-except
            return exception, rpc2


# ---------------------------------------------------------------------------
# What aiohttp does, pinned against the library
# ---------------------------------------------------------------------------


async def test_the_camera_shape_refuses_from_the_request_not_from_the_status(
    socket_enabled,
):
    """The measurement the fix is built on, kept where it can redden.

    `session.get()` is what raises, so an `except` placed around
    `raise_for_status()` -- which is where this fallback used to be -- never
    sees this camera at all. If a future aiohttp moves the refusal to the body
    read instead, this is the test that says so rather than a reporter.
    """
    async with _serving(UNREADABLE) as server:
        async with aiohttp.ClientSession() as session:
            with pytest.raises(aiohttp.ClientResponseError) as raised:
                await session.get("http://127.0.0.1:%d/x" % server.port)

    assert raised.value.status == 400
    assert client_module.refusal_is_mangled_framing(raised.value) is True


async def test_a_correctly_framed_400_survives_the_request(socket_enabled):
    """So the other half of the discriminator is driven through aiohttp too.

    A device's own 400 arrives as a response. `raise_for_status` is what refuses
    it, the error carries headers, and it must keep falling back the way it
    always has instead of going to ask RPC2.
    """
    async with _serving(A_REAL_400) as server:
        async with aiohttp.ClientSession() as session:
            response = await session.get("http://127.0.0.1:%d/x" % server.port)
            with pytest.raises(aiohttp.ClientResponseError) as raised:
                response.raise_for_status()
            response.close()

    assert raised.value.status == 400
    assert client_module.refusal_is_mangled_framing(raised.value) is False


# ---------------------------------------------------------------------------
# The routing
# ---------------------------------------------------------------------------


async def test_an_unreadable_config_answer_is_read_over_rpc2(socket_enabled):
    """The fix. Without it this returns the refusal and the caller gets `{}`."""
    async with _serving(UNREADABLE) as server:
        answer, rpc2 = await _read(server)

    assert answer == RPC2_TABLE
    assert rpc2.asked == ["MotionDetect"]


async def test_the_caller_gets_a_table_instead_of_an_empty_answer(socket_enabled):
    """Through the public read, because `{}` is what the user actually sees.

    `async_get_config` catches `ClientResponseError` and answers `{}`. That
    guard is right -- one unsupported table must not take a device down -- and
    it is also why this camera has looked healthy while knowing nothing.
    """
    async with _serving(UNREADABLE) as server:
        async with aiohttp.ClientSession() as session:
            client = DahuaClient("u", "p", "127.0.0.1", server.port, 554, session)
            client._rpc2_get_config = _Rpc2Reads()

            assert await client.async_get_config("MotionDetect") == RPC2_TABLE


async def test_the_host_is_remembered_so_the_next_read_does_not_pay_again(
    socket_enabled,
):
    """One unreadable answer, and config reads for that host go straight to RPC2.

    Two different tables, because the shared read cache would answer the same
    one twice without asking anything.
    """
    async with _serving(UNREADABLE) as server:
        async with aiohttp.ClientSession() as session:
            client = DahuaClient("u", "p", "127.0.0.1", server.port, 554, session)
            rpc2 = _Rpc2Reads()
            client._rpc2_get_config = rpc2

            assert await client._request(MOTION_DETECT) == RPC2_TABLE
            assert await client._request(VIDEO_IN_MODE) == RPC2_TABLE

    assert rpc2.asked == ["MotionDetect", "VideoInMode"]
    # One round trip, not two: the second read never reached the camera.
    assert len(server.requests) == 1


async def test_the_memo_names_this_host(socket_enabled):
    async with _serving(UNREADABLE) as server:
        await _read(server)

    assert client_module._HOST_CGI_CONFIG_ABSENT, "nothing was remembered"


async def test_an_absent_endpoint_still_routes(socket_enabled):
    """The behaviour this route was built for, unchanged by widening it."""
    async with _serving(A_REAL_404) as server:
        answer, rpc2 = await _read(server)

    assert answer == RPC2_TABLE
    assert rpc2.asked == ["MotionDetect"]


async def test_a_real_400_from_the_device_is_not_routed(socket_enabled):
    """The whole reason the status cannot be the test.

    A recorder answers a real 400 for a channel it does not have. Asking RPC2
    for that would turn one honest refusal into a second round trip on every
    poll, and `_HOST_CGI_CONFIG_ABSENT` would move the whole host's config onto
    RPC2 on the strength of it.
    """
    async with _serving(A_REAL_400) as server:
        answer, rpc2 = await _read(server)

    assert isinstance(answer, aiohttp.ClientResponseError)
    assert answer.status == 400
    assert rpc2.asked == []
    assert not client_module._HOST_CGI_CONFIG_ABSENT


async def test_a_readable_answer_is_not_routed(socket_enabled):
    """The ordinary case, so the fallback is not standing in front of it."""
    async with _serving(A_CLEAN_200) as server:
        answer, rpc2 = await _read(server)

    assert answer == {"table.MotionDetect[0].Enable": "true"}
    assert rpc2.asked == []


async def test_a_non_config_read_is_not_routed(socket_enabled):
    """An identity read is answered by the identity fallback, not by this one.

    `_CONFIG_READ` matches `configManager.cgi?action=getConfig&name=`, and
    `magicBox.cgi` has its own route to RPC2 (#1183) that knows how to turn the
    answer into a model, a class and a serial. Sending it here would ask for a
    config table called `getMachineName`.
    """
    async with _serving(UNREADABLE) as server:
        answer, rpc2 = await _read(server, url=MACHINE_NAME)

    assert isinstance(answer, aiohttp.ClientResponseError)
    assert rpc2.asked == []
    assert not client_module._HOST_CGI_CONFIG_ABSENT


async def test_a_write_is_never_routed(socket_enabled):
    """`verify_ok` is a write. A config read cannot stand in for one.

    Routing a write here would answer "did the camera accept this?" with the
    contents of a table, which is the shape of a device saying no in the other
    voice -- a 200 carrying `Error` read as success.
    """
    async with _serving(UNREADABLE) as server:
        answer, rpc2 = await _read(server, verify_ok=True)

    assert isinstance(answer, aiohttp.ClientResponseError)
    assert rpc2.asked == []


async def test_a_caller_that_asked_for_cgi_only_is_not_routed(socket_enabled):
    """`allow_rpc2=False` is how the per-rule IVS reads pin themselves to CGI."""
    async with _serving(UNREADABLE) as server:
        answer, rpc2 = await _read(server, allow_rpc2=False)

    assert isinstance(answer, aiohttp.ClientResponseError)
    assert rpc2.asked == []


async def test_a_host_whose_rpc2_does_not_work_is_not_routed(socket_enabled):
    """Nothing to route to. The refusal is reported as it is today."""
    # The key's shape is the client's business, so it is taken from the client
    # rather than guessed: whatever `_rpc2_key` returns is what the gate reads.
    async with _serving(UNREADABLE) as server:
        async with aiohttp.ClientSession() as session:
            client = DahuaClient("u", "p", "127.0.0.1", server.port, 554, session)
            client_module._HOST_RPC2_UNAVAILABLE.add(client._rpc2_key())
            rpc2 = _Rpc2Reads()
            client._rpc2_get_config = rpc2

            with pytest.raises(aiohttp.ClientResponseError):
                await client._request(MOTION_DETECT)

    assert rpc2.asked == []


async def test_a_table_both_transports_have_refused_is_not_asked_again(
    socket_enabled,
):
    async with _serving(UNREADABLE) as server:
        async with aiohttp.ClientSession() as session:
            client = DahuaClient("u", "p", "127.0.0.1", server.port, 554, session)
            client_module._RPC2_TABLE_UNAVAILABLE.add(
                (client._rpc2_key(), "MotionDetect")
            )
            rpc2 = _Rpc2Reads()
            client._rpc2_get_config = rpc2

            with pytest.raises(aiohttp.ClientResponseError):
                await client._request(MOTION_DETECT)

    assert rpc2.asked == []


# ---------------------------------------------------------------------------
# The floor: routing must not be able to make this camera worse
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "failure",
    [
        TimeoutError("RPC2 did not answer"),
        ConnectionResetError("RPC2 session dropped"),
        ValueError("something nobody predicted"),
    ],
    ids=["timeout", "reset", "unpredicted"],
)
async def test_an_rpc2_failure_reports_the_original_refusal(socket_enabled, failure):
    """And that matters more than it looks.

    Today every read on this camera is swallowed by the `ClientResponseError`
    guard in `async_get_config`, which hands back `{}`. A failure escaping from
    the fallback as something that is **not** a `ClientError` would get past
    that guard, and the poll's `gather` has no `return_exceptions`, so one RPC2
    timeout would take the whole entry to `setup_retry` -- strictly worse than
    the badly populated entry the fix is trying to improve on.
    """
    async with _serving(UNREADABLE) as server:
        answer, rpc2 = await _read(server, rpc2=_Rpc2Reads(raises=failure))

    assert rpc2.asked == ["MotionDetect"]
    assert isinstance(answer, aiohttp.ClientResponseError), type(answer).__name__
    assert answer.status == 400


async def test_the_caller_still_gets_an_empty_answer_when_rpc2_cannot_help(
    socket_enabled,
):
    """The same thing said where the user meets it: `{}`, not a failed refresh."""
    async with _serving(UNREADABLE) as server:
        async with aiohttp.ClientSession() as session:
            client = DahuaClient("u", "p", "127.0.0.1", server.port, 554, session)
            client._rpc2_get_config = _Rpc2Reads(raises=TimeoutError("no answer"))

            assert await client.async_get_config("MotionDetect") == {}


async def test_a_refused_table_is_remembered_so_it_is_not_asked_twice(socket_enabled):
    """An RPC2 refusal is the device's verdict on the table, and it is kept.

    `Rpc2MethodRefused` already had this path. It is here because the widening
    brings a second kind of device to it, and a fallback that cannot help has to
    stop costing a round trip.
    """
    refusal = client_module.Rpc2MethodRefused("no such table")
    async with _serving(UNREADABLE) as server:
        async with aiohttp.ClientSession() as session:
            client = DahuaClient("u", "p", "127.0.0.1", server.port, 554, session)
            client._rpc2_get_config = _Rpc2Reads(raises=refusal)

            with pytest.raises(aiohttp.ClientResponseError):
                await client._request(MOTION_DETECT)

            assert (
                client._rpc2_key(),
                "MotionDetect",
            ) in client_module._RPC2_TABLE_UNAVAILABLE


# ---------------------------------------------------------------------------
# What the log says
# ---------------------------------------------------------------------------


async def test_the_log_does_not_claim_the_device_serves_no_cgi_config(
    socket_enabled, caplog
):
    """The existing wording is false for this camera, and misleadingly so.

    @defre92 raised the same thing about the identity warning: a line that says
    something untrue about a device sends the next reader looking for the wrong
    fault. This camera's `configManager.cgi` is there and answers; it is the
    framing of the answer that nothing can read.
    """
    async with _serving(UNREADABLE) as server:
        await _read(server)

    logged = "\n".join(
        record.message for record in caplog.records if record.levelname == "INFO"
    )

    assert "no HTTP parser accepts" in logged, logged
    assert "serves no CGI config" not in logged
    assert "#1176" in logged


async def test_an_absent_endpoint_still_says_so(socket_enabled, caplog):
    """The other wording is still reached, which a single `if` could have lost."""
    async with _serving(A_REAL_404) as server:
        await _read(server)

    logged = "\n".join(
        record.message for record in caplog.records if record.levelname == "INFO"
    )

    assert "serves no CGI config" in logged, logged
    assert "no HTTP parser accepts" not in logged


# ---------------------------------------------------------------------------
# One consequence worth stating: the device NAME is a config read too
# ---------------------------------------------------------------------------

DEVICE_NAME = "/cgi-bin/configManager.cgi?action=getConfig&name=General.MachineName"


async def test_the_device_name_read_is_routed_like_any_other_config_read(
    socket_enabled,
):
    """Recorded rather than asserted as a good thing, because it is unmeasured.

    `async_get_machine_name` reads `configManager.cgi&name=General.MachineName`,
    so widening this fallback sends it to RPC2 as a table called
    `General.MachineName`. Whether a Dahua device serves a dotted field name
    over RPC2 is not something anybody has measured on #1176's firmware. If it
    does, that camera gets its real name instead of a hash of its credentials.
    If it refuses, the test below is what happens. Both are safe, which is why
    this does not wait for the answer.
    """
    async with _serving(UNREADABLE) as server:
        answer, rpc2 = await _read(server, url=DEVICE_NAME)

    assert rpc2.asked == ["General.MachineName"]
    assert answer == RPC2_TABLE


async def test_the_device_name_falls_back_to_the_hash_when_rpc2_refuses_it(
    socket_enabled,
):
    """The floor for the name, which has teeth the other reads do not.

    A name is cosmetic, but `identity_derived_from_credentials` is not: it is
    the flag that says this device's identity moves when its password does
    (#805). The fallback has to leave it exactly where today's refusal leaves
    it, so a device that cannot be named is not quietly recorded as one that
    was.
    """
    refusal = client_module.Rpc2MethodRefused("no such table")
    async with _serving(UNREADABLE) as server:
        async with aiohttp.ClientSession() as session:
            client = DahuaClient("u", "p", "127.0.0.1", server.port, 554, session)
            client._rpc2_get_config = _Rpc2Reads(raises=refusal)

            answer = await client.async_get_machine_name()

    assert answer == {"table.General.MachineName": client.credential_hash()}
    assert client.identity_derived_from_credentials is True


async def test_an_rpc2_read_that_hangs_reports_the_original_refusal(
    socket_enabled, monkeypatch
):
    """The failure the three above do NOT model, and the one that can happen.

    Those raise from the double, so `except Exception` catches them. A real
    hang does not raise: the request's own `asyncio.timeout` expires, the task
    is cancelled, and `CancelledError` is not an `Exception`. Without a
    deadline of its own the fallback is cut off by the outer one and the caller
    gets a `TimeoutError`, which `async_get_config` does not catch and the
    poll's `gather` does not tolerate.
    """
    monkeypatch.setattr(client_module, "TIMEOUT_SECONDS", 0.4)

    async def hangs(name):
        await asyncio.sleep(5)

    async with _serving(UNREADABLE) as server:
        answer, _ = await _read(server, rpc2=hangs)

    assert isinstance(answer, aiohttp.ClientResponseError), type(answer).__name__
    assert answer.status == 400
