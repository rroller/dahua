"""A still goes out the way 0.9.84 and curl send it (#1197).

From 0.9.85 every request signed itself up front with the challenge the
device last handed any request. A DHI-NVR2108HS-8P-S2 on 3.205 stopped
setting up on 0.9.85 and set up again on 0.9.84, and on 3.x its stills and
the setup still time out while curl's are answered in under a second. A
still, and the probe that is also a still, now start with no credentials
and answer the 401 they get, as they did before the challenge was shared.
The nonce count stays the shared one, so a device that repeats its nonce
and refuses a reused count still accepts them (rroller on #1229).
"""

import asyncio

import pytest

from custom_components.dahua import client as client_module
from custom_components.dahua.client import DahuaClient

from .test_digest import PASSWORD, USER, FakeSession, nc_of

READ = "/cgi-bin/magicBox.cgi?action=getSystemInfo"


@pytest.fixture(autouse=True)
def _clean_host_state():
    client_module._HOST_DIGEST_STATE.clear()
    client_module._HOST_LIMITS.clear()
    client_module._HOST_CACHE.clear()
    yield
    client_module._HOST_DIGEST_STATE.clear()
    client_module._HOST_LIMITS.clear()
    client_module._HOST_CACHE.clear()


def _client(session):
    return DahuaClient(USER, PASSWORD, "10.0.0.1", 80, 554, session)


def _snapshot_requests(session):
    return [r for r in session.requests if "snapshot.cgi" in str(r["url"])]


async def _warmed(**session_kwargs):
    """A client whose host already holds a challenge, as it does after the
    first poll, with the device's count moved on past 1."""
    session = FakeSession(body="jpeg", **session_kwargs)
    client = _client(session)
    await client.get(READ)
    await client.get(READ + "&again=1")
    assert client._digest_state.get("challenge"), "the read should leave one"
    assert client._digest_state["nonce_count"] == 2
    return session, client


async def test_a_still_starts_without_credentials():
    session, client = await _warmed()

    image = await client.async_get_snapshot(1)

    first, second = _snapshot_requests(session)
    assert "AUTHORIZATION" not in first["headers"]
    assert "AUTHORIZATION" in second["headers"]
    assert image == b"jpeg"


async def test_the_setup_probe_starts_without_credentials():
    session, client = await _warmed()

    await client.async_probe_snapshot(0)

    first, second = _snapshot_requests(session)
    assert "AUTHORIZATION" not in first["headers"]
    assert "AUTHORIZATION" in second["headers"]


# --- a repeated nonce keeps its count (rroller on #1229) ----------------------
# FakeSession hands out one nonce for ever, as our DHI-NVR5464 and VTO2000A do,
# and with strict_nc it refuses a count it has already seen, as RFC 7616 allows.
# The two reads have used counts 1 and 2, so a still that started its own count
# at 1 would be refused on both signed attempts.


async def test_a_still_carries_on_the_count_of_a_repeated_nonce():
    session, client = await _warmed(strict_nc=True)

    assert await client.async_get_snapshot(1) == b"jpeg"

    _first, second = _snapshot_requests(session)
    assert nc_of(second) == "00000003"


async def test_the_setup_probe_carries_on_the_count_of_a_repeated_nonce():
    session, client = await _warmed(strict_nc=True)

    await client.async_probe_snapshot(0)

    _first, second = _snapshot_requests(session)
    assert nc_of(second) == "00000003"


async def test_a_read_after_a_still_carries_on_the_count_too():
    """The still's count is the reads' count, in both directions."""
    session, client = await _warmed(strict_nc=True)
    await client.async_get_snapshot(1)

    assert await client.get(READ + "&after=1") is not None
    assert nc_of(session.requests[-1]) == "00000004"


async def test_other_raw_reads_still_sign_up_front():
    """Only stills ask first. The event index read goes through the same
    get_bytes and keeps signing up front, so it costs no extra round trip."""
    session, client = await _warmed()
    sent = len(session.requests)

    await client.get_bytes("/cgi-bin/eventManager.cgi?action=getEventIndexes&code=X")

    (only,) = session.requests[sent:]
    assert "AUTHORIZATION" in only["headers"]


async def test_a_device_that_speaks_basic_still_gets_its_credentials():
    """Basic has no challenge to reuse, so a still sends it up front as
    before. Waiting for a 401 would hand back a refusal, because a device
    already known to want Basic answering 401 Basic is a wrong password."""
    from custom_components.dahua.digest import BASIC, DigestAuth

    class _Basic:
        def __init__(self):
            self.sent = []

        async def request(self, method, url, headers=None, **kwargs):
            self.sent.append(dict(headers or {}))
            ok = "AUTHORIZATION" in (headers or {})
            response = type("R", (), {})()
            response.status = 200 if ok else 401
            response.headers = {} if ok else {"www-authenticate": 'Basic realm="x"'}
            response.close = lambda: None
            return response

    session = _Basic()
    auth = DigestAuth(USER, PASSWORD, session, {"scheme": BASIC})

    response = await auth.request(
        "GET", "http://d/cgi-bin/snapshot.cgi", preemptive=False
    )

    assert response.status == 200
    assert session.sent[0]["AUTHORIZATION"].startswith("Basic ")


class _RecorderThatIgnoresAStillSignedUpFront(FakeSession):
    """What #1197's recorder looks like from outside, as far as is known: a
    still signed before the recorder challenged it is never answered. A still
    that asked first, took the 401, then signed, is."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.challenged = set()

    async def request(self, method, url, headers=None, **kwargs):
        headers = headers or {}
        if "snapshot.cgi" in str(url):
            if "AUTHORIZATION" not in headers:
                self.challenged.add(str(url))
            elif str(url) not in self.challenged:
                self.requests.append(
                    {"method": method, "url": url, "headers": dict(headers)}
                )
                await asyncio.Event().wait()
            else:
                self.challenged.discard(str(url))
        return await super().request(method, url, headers=headers, **kwargs)


async def test_that_recorder_answers_a_still(monkeypatch):
    monkeypatch.setattr(client_module, "TIMEOUT_SECONDS", 1)
    session = _RecorderThatIgnoresAStillSignedUpFront(body="jpeg")
    client = _client(session)
    await client.get(READ)
    await client.get(READ + "&again=1")

    assert await client.async_get_snapshot(1) == b"jpeg"
    await client.async_probe_snapshot(0)
