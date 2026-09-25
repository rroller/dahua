"""Tests for custom_components.dahua.digest."""
import asyncio
import hashlib
import re

import pytest

from custom_components.dahua import client as client_module
from custom_components.dahua.client import DahuaClient
from custom_components.dahua.digest import DigestAuth

REALM = "DahuaRpc"
USER = "admin"
PASSWORD = "secret"


@pytest.fixture(autouse=True)
def _clean_host_digest_state():
    """A challenge is now shared per host, so it outlives a test unless cleared."""
    client_module._HOST_DIGEST_STATE.clear()
    yield
    client_module._HOST_DIGEST_STATE.clear()


def _params(header: str) -> dict:
    """Parse the parameters out of an Authorization header."""
    body = header.partition(" ")[2]
    return {m[0]: (m[1] or m[2])
            for m in re.findall(r'(\w+)=(?:"([^"]*)"|([^,\s]+))', body)}


def _expected_response(method: str, params: dict, password: str) -> str:
    """Recompute the digest the client should have sent."""
    def h(value):
        return hashlib.md5(value.encode()).hexdigest()

    ha1 = h("%s:%s:%s" % (params.get("username", ""), REALM, password))
    ha2 = h("%s:%s" % (method, params.get("uri", "")))
    if params.get("qop"):
        return h(":".join([ha1, params.get("nonce", ""), params.get("nc", ""),
                           params.get("cnonce", ""), "auth", ha2]))
    return h("%s:%s:%s" % (ha1, params.get("nonce", ""), ha2))


class FakeResponse:
    def __init__(self, status, headers=None, body=""):
        self.status = status
        self.headers = headers or {}
        self.closed = False
        self._body = body

    def close(self):
        self.closed = True

    async def text(self):
        return self._body

    async def read(self):
        return self._body.encode()

    def raise_for_status(self):
        if self.status >= 400:
            raise AssertionError("unexpected %s in this test" % self.status)


class FakeSession:
    """A device that actually verifies the digest it is sent.

    Requests suspend before replying so concurrent callers genuinely overlap
    rather than each running to completion in one scheduler step.
    """

    def __init__(self, password=PASSWORD, nonce="nonce-1", strict_nc=False, body="ok=1"):
        self.requests = []
        self.password = password
        self.nonce = nonce
        self.strict_nc = strict_nc
        self.body = body
        self.seen_nc = set()

    def _challenge(self, stale=False):
        header = 'Digest realm="%s", nonce="%s", qop="auth"' % (REALM, self.nonce)
        if stale:
            header += ', stale="true"'
        return {"www-authenticate": header}

    async def request(self, method, url, headers=None, **kwargs):
        headers = headers or {}
        self.requests.append({"method": method, "url": url, "headers": dict(headers)})
        await asyncio.sleep(0)  # let siblings interleave

        auth = headers.get("AUTHORIZATION")
        if not auth:
            return FakeResponse(401, self._challenge())

        params = _params(auth)
        if params.get("nonce") != self.nonce:
            return FakeResponse(401, self._challenge(stale=True))
        if params.get("response") != _expected_response(method.upper(), params, self.password):
            return FakeResponse(401, self._challenge())
        if self.strict_nc:
            nc = params.get("nc")
            if nc in self.seen_nc:
                # RFC 7616 5.5: a replayed count is refused with the same nonce.
                return FakeResponse(401, self._challenge())
            self.seen_nc.add(nc)

        return FakeResponse(200, body=self.body)


def nc_of(request):
    match = re.search(r"nc=([0-9a-f]{8})", request["headers"].get("AUTHORIZATION", ""))
    return match.group(1) if match else None


async def test_challenge_is_reused_across_requests():
    """The first call absorbs a 401; later calls authenticate up front."""
    session = FakeSession()
    state = {}

    first = await DigestAuth(USER, PASSWORD, session, state).request("GET", "http://d/one")
    assert first.status == 200
    assert len(session.requests) == 2  # probe, then authenticated retry

    second = await DigestAuth(USER, PASSWORD, session, state).request("GET", "http://d/two")
    assert second.status == 200
    assert len(session.requests) == 3  # no second probe
    assert "AUTHORIZATION" in session.requests[2]["headers"]


async def test_client_reuses_one_challenge_across_calls():
    """The client must thread one challenge through every call site."""
    session = FakeSession(body="deviceType=NVR")
    client = DahuaClient(USER, PASSWORD, "d", 80, 554, session)

    await client.get("/cgi-bin/magicBox.cgi?action=getSystemInfo")
    await client.get("/cgi-bin/magicBox.cgi?action=getDeviceType")

    # Four requests would mean each call re-challenged independently.
    assert len(session.requests) == 3


async def test_without_shared_state_every_request_rechallenges():
    """Callers that pass no state keep the old behaviour."""
    session = FakeSession()

    for _ in range(3):
        await DigestAuth(USER, PASSWORD, session).request("GET", "http://d/x")

    assert len(session.requests) == 6  # two per call


async def test_nonce_count_increments_across_requests():
    """nc must advance per RFC 2617 while the nonce is unchanged."""
    session = FakeSession()
    state = {}

    for _ in range(3):
        await DigestAuth(USER, PASSWORD, session, state).request("GET", "http://d/x")

    assert [nc_of(r) for r in session.requests if nc_of(r)] == [
        "00000001", "00000002", "00000003"]


async def test_concurrent_requests_get_distinct_nonce_counts():
    """Requests genuinely in flight together must not reuse a count."""
    session = FakeSession()
    state = {}

    await DigestAuth(USER, PASSWORD, session, state).request("GET", "http://d/prime")
    before = len(session.requests)

    results = await asyncio.gather(*[
        DigestAuth(USER, PASSWORD, session, state).request("GET", "http://d/%d" % i)
        for i in range(5)
    ])

    assert [r.status for r in results] == [200] * 5
    counts = [nc_of(r) for r in session.requests[before:]]
    assert len(set(counts)) == len(counts) == 5, "nonce counts collided: %s" % counts


async def test_out_of_order_nonce_count_recovers():
    """A device refusing a replayed count must not fail the call outright."""
    session = FakeSession(strict_nc=True)
    state = {}
    await DigestAuth(USER, PASSWORD, session, state).request("GET", "http://d/prime")

    # Replay the count the priming call already burned.
    state["last_nonce"] = ""
    state["nonce_count"] = 0

    response = await DigestAuth(USER, PASSWORD, session, state).request("GET", "http://d/x")
    assert response.status == 200


async def test_rejected_credentials_stop_after_budget():
    """A 401 that keeps its challenge header must not retry forever."""
    session = FakeSession(password="something-else")

    response = await DigestAuth(USER, "wrong", session, {}).request("GET", "http://d/x")

    assert response.status == 401
    assert len(session.requests) <= 3


async def test_stale_challenge_is_refreshed_and_succeeds():
    """A rotated nonce is re-learned and the call still succeeds."""
    session = FakeSession(nonce="nonce-1")
    state = {}
    await DigestAuth(USER, PASSWORD, session, state).request("GET", "http://d/x")
    assert state["challenge"]["nonce"] == "nonce-1"

    session.nonce = "nonce-2"
    before = len(session.requests)

    response = await DigestAuth(USER, PASSWORD, session, state).request("GET", "http://d/y")
    assert response.status == 200
    assert state["challenge"]["nonce"] == "nonce-2"
    assert len(session.requests) - before == 2  # stale attempt, then success


async def test_unusable_cached_challenge_is_discarded():
    """A cached challenge missing required fields must not poison later calls."""
    session = FakeSession()
    state = {"challenge": {"qop": "auth"}}  # no realm, no nonce

    response = await DigestAuth(USER, PASSWORD, session, state).request("GET", "http://d/x")

    assert response.status == 200
    assert state["challenge"]["nonce"] == session.nonce
