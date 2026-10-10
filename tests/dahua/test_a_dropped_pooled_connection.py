"""A connection the device drops must not take the whole poll with it.

#1001, measured on an Intelbras VIPW-1300-MINI-SD (Dahua OEM) running
2.820.00IB004.0.T: roughly every 62 minutes, and sometimes every 31, the camera
closes the keep-alive connection the integration is reusing. The next request
on it fails with `[Errno 104] Connection reset by peer` or `Server
disconnected`, and because nothing retried, that one dead socket aborted
`_async_update_data`, raised `UpdateFailed`, and put **every entity of the
device** into `unavailable` for a full polling interval. 17 times in 24 hours.
The next poll always recovered on its own, which is the shape of the bug: there
was never anything wrong with the device or the credentials.

The poll ran every 30 seconds throughout, so the connection was never idle.
That rules out the fix that would otherwise be preferable -- closing it
ourselves before the device does -- because there is no idle period to shorten,
and aiohttp has no maximum connection lifetime to set below the device's.

So the fix is to ask again. Both of these exceptions are raised before any byte
of a response arrives, so the request was never answered: the only thing lost
is the socket, and a second attempt opens a new one.

Put at the two places that issue requests rather than in the poll, which is
where the reporter's own tested patch put it. The poll is not the only caller
that meets a dead socket -- a snapshot, an infrared write or a light turning on
hits the same pool, and in the poll-only version each of those still fails
visibly. One of these two chokepoints carries every CGI request
(`DigestAuth.request`) and the other every RPC2 one (`DahuaRpc2Client.request`,
which is the path #1001 was reported on).

A retried write is safe here for the reason above: the device never answered,
and a Dahua config write is idempotent anyway -- setting a field to the value
it was already being set to is the same request, not a second one.

What the mutation is, for anyone reintroducing it: make
`is_stale_pooled_connection` return False and
`test_a_reset_connection_is_asked_again_and_succeeds` fails with the
ClientOSError propagating, which is #1001 exactly.
"""

import asyncio
import errno

import aiohttp
from multidict import CIMultiDict
import pytest

from custom_components.dahua.digest import (
    MAX_AUTH_ATTEMPTS,
    DigestAuth,
    is_stale_pooled_connection,
)
from custom_components.dahua.rpc2 import DahuaRpc2Client

USER = "admin"
PASSWORD = "secret"

# What aiohttp delivers for the reported failure. ECONNRESET rather than the
# literal 104, which is Linux's number -- it is 10054 on Windows, and a test
# pinning 104 passes for the wrong reason on one of them.
RESET = (errno.ECONNRESET, "Connection reset by peer")


class _Response:
    def __init__(self, status=200, body="ok=1"):
        self.status = status
        # The real thing is a CIMultiDict, and digest.py reads
        # `headers.get("www-authenticate")` in lower case. A plain dict is
        # case sensitive, so a fake using one makes a challenge invisible and
        # the test passes for the wrong reason.
        self.headers = CIMultiDict()
        self._body = body

    def close(self):
        pass

    async def text(self):
        return self._body


class _Session:
    """Raises the given errors in order, one per request, then answers.

    `calls` is the point of every test here: the fix is one extra attempt, not
    a loop, and "it eventually worked" is not the same claim.
    """

    def __init__(self, *errors, response=None):
        self.errors = list(errors)
        self.response = response
        self.calls = 0

    async def request(self, method, url, headers=None, **kwargs):
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return self.response or _Response()

    async def post(self, url, json=None, **kwargs):
        response = await self.request("POST", url)
        # RPC2 parses the body before it looks at anything else, so a
        # non-JSON one fails the call for a reason that has nothing to do
        # with the connection under test.
        response._body = '{"id": 1, "result": true}'
        return response


def _auth(session):
    return DigestAuth(USER, PASSWORD, session, {})


def _rpc2(session):
    return DahuaRpc2Client(USER, PASSWORD, "10.0.0.1", 80, 554, session)


# --- the CGI path, which every read and write goes through ------------------


async def test_a_reset_connection_is_asked_again_and_succeeds():
    """The reported failure, and the whole point: the second attempt answers."""
    session = _Session(aiohttp.ClientOSError(*RESET))

    response = await _auth(session).request("GET", "http://d/cgi-bin/x.cgi")

    assert response.status == 200
    assert session.calls == 2


async def test_a_server_disconnect_is_asked_again():
    """The other shape in the report. Not an OSError at all -- it descends from
    ClientError only -- so an `except OSError` would never see it."""
    session = _Session(aiohttp.ServerDisconnectedError())

    response = await _auth(session).request("GET", "http://d/cgi-bin/x.cgi")

    assert response.status == 200
    assert session.calls == 2


async def test_a_reset_with_no_errno_is_asked_again():
    """A bare ConnectionResetError carries `errno = None`, so matching on the
    number alone misses it. Matched by class for that reason."""
    session = _Session(ConnectionResetError())

    response = await _auth(session).request("GET", "http://d/cgi-bin/x.cgi")

    assert response.status == 200
    assert session.calls == 2


async def test_a_reset_wrapped_in_another_error_is_asked_again():
    """aiohttp wraps the socket's OSError, and asyncio sometimes wraps that
    again, so the errno is usually not on the exception that arrives."""
    wrapped = aiohttp.ClientConnectionError("transport closed")
    wrapped.__cause__ = ConnectionResetError(*RESET)
    session = _Session(wrapped)

    response = await _auth(session).request("GET", "http://d/cgi-bin/x.cgi")

    assert response.status == 200
    assert session.calls == 2


# --- and the things that must still fail ------------------------------------


async def test_a_device_that_is_really_gone_still_fails():
    """One extra attempt, not a loop. A camera that is off must still go
    unavailable, and must not take a polling interval to decide it."""
    session = _Session(aiohttp.ClientOSError(*RESET), aiohttp.ClientOSError(*RESET))

    with pytest.raises(aiohttp.ClientOSError):
        await _auth(session).request("GET", "http://d/cgi-bin/x.cgi")

    assert session.calls == 2, "the second failure must be raised, not retried"


async def test_a_refused_connection_is_not_retried():
    """ECONNREFUSED is a device that is not listening, not a stale socket. It
    shares a base class with the reset (ClientConnectorError is a ClientOSError)
    which is why this is checked rather than assumed."""
    refused = aiohttp.ClientConnectorError.__new__(aiohttp.ClientConnectorError)
    OSError.__init__(refused, errno.ECONNREFUSED, "Connection refused")
    session = _Session(refused)

    with pytest.raises(OSError):
        await _auth(session).request("GET", "http://d/cgi-bin/x.cgi")

    assert session.calls == 1


async def test_a_timeout_is_not_retried_here():
    """A timeout may mean the device is thinking, and the layers above already
    decide what to do about one. Retrying it here would double the wait before
    they get the chance."""
    session = _Session(asyncio.TimeoutError())

    with pytest.raises(asyncio.TimeoutError):
        await _auth(session).request("GET", "http://d/cgi-bin/x.cgi")

    assert session.calls == 1


async def test_a_dropped_connection_does_not_spend_the_auth_budget():
    """Why this is separate from the digest loop rather than a `continue` in it.

    A device may drop a connection *and* ask for a challenge. Spending one of
    MAX_AUTH_ATTEMPTS on the dropped socket would leave fewer attempts than the
    401 needs, and the user would be told their password was refused.
    """
    challenge = _Response(status=401, body="")
    challenge.headers = CIMultiDict(
        {"WWW-Authenticate": 'Digest realm="DahuaRpc", nonce="n1", qop="auth"'}
    )
    session = _Session(aiohttp.ClientOSError(*RESET), response=challenge)
    # After the reset, every attempt gets the 401 above, so this spends the
    # whole auth budget and nothing more.
    await _auth(session).request("GET", "http://d/cgi-bin/x.cgi")

    assert session.calls == 1 + MAX_AUTH_ATTEMPTS, (
        "the dropped connection cost an authentication attempt: %d calls"
        % session.calls
    )


# --- the RPC2 path, which is where #1001 was reported -----------------------


async def test_an_rpc2_reset_is_asked_again():
    session = _Session(aiohttp.ClientOSError(*RESET))

    await _rpc2(session).request("magicBox.getSystemInfo", verify_result=False)

    assert session.calls == 2


async def test_an_rpc2_device_that_is_really_gone_still_fails():
    session = _Session(aiohttp.ClientOSError(*RESET), aiohttp.ClientOSError(*RESET))

    with pytest.raises(aiohttp.ClientOSError):
        await _rpc2(session).request("magicBox.getSystemInfo", verify_result=False)

    assert session.calls == 2


# --- the predicate on its own -----------------------------------------------


def test_an_ordinary_client_error_is_not_a_dropped_connection():
    assert is_stale_pooled_connection(aiohttp.ClientError()) is False


def test_a_cycle_in_the_cause_chain_terminates():
    """__cause__ chains can loop. Walking one without a bound hangs the poll,
    and a hang is worse than the bug this fixes."""
    first = aiohttp.ClientError("first")
    second = aiohttp.ClientError("second")
    first.__cause__ = second
    second.__cause__ = first

    assert is_stale_pooled_connection(first) is False
