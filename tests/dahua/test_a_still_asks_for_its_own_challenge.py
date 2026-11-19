"""A still goes out the way 0.9.84 and curl send it (#1197).

From 0.9.85 every request signed itself up front with the challenge the
device last handed any request. A DHI-NVR2108HS-8P-S2 on 3.205 stopped
setting up on 0.9.85 and set up again on 0.9.84, and on 3.x its stills and
the setup still time out while curl's are answered in under a second. A
still, and the probe that is also a still, now start with no credentials
and answer the 401 they get, as they did before the challenge was shared.
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


async def _warmed():
    """A client whose host already holds a challenge, as it does after the
    first poll, with the device's count moved on past 1."""
    session = FakeSession(body="jpeg")
    client = _client(session)
    await client.get(READ)
    await client.get(READ + "&again=1")
    assert client._digest_state.get("challenge"), "the read should leave one"
    assert client._digest_state["nonce_count"] > 1
    return session, client


async def test_a_still_starts_without_the_shared_challenge():
    session, client = await _warmed()

    image = await client.async_get_snapshot(1)

    first, second = _snapshot_requests(session)
    assert "AUTHORIZATION" not in first["headers"]
    assert nc_of(second) == "00000001"
    assert image == b"jpeg"


async def test_the_setup_probe_starts_without_the_shared_challenge():
    session, client = await _warmed()

    await client.async_probe_snapshot(0)

    first, second = _snapshot_requests(session)
    assert "AUTHORIZATION" not in first["headers"]
    assert nc_of(second) == "00000001"


async def test_a_still_leaves_the_shared_challenge_to_the_reads():
    """The reads keep their own count, so a still does not move it under them."""
    _session, client = await _warmed()
    before = dict(client._digest_state)

    await client.async_get_snapshot(1)
    await client.async_probe_snapshot(0)

    assert client._digest_state == before


async def test_other_raw_reads_still_share_the_challenge():
    """Only stills ask afresh. The event index read goes through the same
    get_bytes and keeps signing up front, so it costs no extra round trip."""
    session, client = await _warmed()
    sent = len(session.requests)

    await client.get_bytes("/cgi-bin/eventManager.cgi?action=getEventIndexes&code=X")

    (only,) = session.requests[sent:]
    assert "AUTHORIZATION" in only["headers"]


class _RecorderThatIgnoresAReusedChallengeOnAStill(FakeSession):
    """What #1197's recorder looks like from outside: a still signed with a
    challenge it did not just issue for that still is never answered."""

    async def request(self, method, url, headers=None, **kwargs):
        headers = headers or {}
        auth = headers.get("AUTHORIZATION")
        if (
            "snapshot.cgi" in str(url)
            and auth
            and nc_of({"headers": headers}) != ("00000001")
        ):
            self.requests.append(
                {"method": method, "url": url, "headers": dict(headers)}
            )
            await asyncio.Event().wait()
        return await super().request(method, url, headers=headers, **kwargs)


async def test_that_recorder_answers_a_still(monkeypatch):
    monkeypatch.setattr(client_module, "TIMEOUT_SECONDS", 1)
    session = _RecorderThatIgnoresAReusedChallengeOnAStill(body="jpeg")
    client = _client(session)
    await client.get(READ)
    await client.get(READ + "&again=1")

    assert await client.async_get_snapshot(1) == b"jpeg"
    await client.async_probe_snapshot(0)
