"""Eleven config entries for one NVR should not each take a 401 to get going."""

import asyncio

import pytest

from custom_components.dahua import client as client_module
from custom_components.dahua.client import (
    MAX_CONCURRENT_REQUESTS_PER_HOST,
    DahuaClient,
)

from .test_digest import PASSWORD, USER, FakeSession, nc_of

URL = "/cgi-bin/magicBox.cgi?action=getSystemInfo"


@pytest.fixture(autouse=True)
def _clean_host_state():
    client_module._HOST_DIGEST_STATE.clear()
    client_module._HOST_LIMITS.clear()
    yield
    client_module._HOST_DIGEST_STATE.clear()
    client_module._HOST_LIMITS.clear()


def _client(session, address="10.0.0.1", username=USER, password=PASSWORD):
    return DahuaClient(username, password, address, 80, 554, session)


def _probes(session):
    """Requests that went out with no Authorization header."""
    return [r for r in session.requests if "AUTHORIZATION" not in r["headers"]]


# --- the burst this exists to remove ---------------------------------------

async def test_the_second_entry_does_not_re_probe():
    session = FakeSession()
    await _client(session).get(URL)
    before = len(session.requests)

    await _client(session).get(URL)

    assert len(session.requests) == before + 1, "the second entry took its own 401"


async def test_the_cold_start_burst_does_not_grow_with_the_channel_count():
    """One NVR, one config entry per channel, all starting up together.

    Entries that are already in flight when the first challenge lands cannot
    use it, so this is not one probe -- it is bounded by how many requests the
    host limiter lets run at once, whether that is eleven channels or thirty.
    """
    session = FakeSession()
    clients = [_client(session) for _ in range(11)]

    await asyncio.gather(*(c.get(URL) for c in clients))

    assert len(_probes(session)) <= MAX_CONCURRENT_REQUESTS_PER_HOST, (
        "%d of 11 entries challenged the device" % len(_probes(session))
    )


async def test_thirty_entries_cost_no_more_probes_than_eleven():
    """The property stated plainly: the burst is capped, not proportional."""
    small, large = FakeSession(), FakeSession()

    await asyncio.gather(*(_client(small).get(URL) for _ in range(11)))
    client_module._HOST_DIGEST_STATE.clear()
    client_module._HOST_LIMITS.clear()
    await asyncio.gather(*(_client(large).get(URL) for _ in range(30)))

    assert len(_probes(large)) <= len(_probes(small))


async def test_every_caller_still_gets_its_answer():
    session = FakeSession(body="key=value")
    clients = [_client(session) for _ in range(11)]

    results = await asyncio.gather(*(c.get(URL) for c in clients))

    assert all(r == {"key": "value"} for r in results)


# --- what must not be shared ------------------------------------------------

async def test_two_hosts_do_not_share_a_challenge():
    """A challenge is the device's, and a nonce from one is nothing to another."""
    a, b = _client(FakeSession()), _client(FakeSession(), "10.0.0.2")

    assert a._digest_state is not b._digest_state


async def test_two_users_on_one_host_do_not_share_a_challenge():
    """The response digest is built from the credentials, so the count that
    goes with a nonce belongs to the user, not just the host."""
    session = FakeSession()
    a = _client(session, username="alice")
    b = _client(session, username="bob")

    assert a._digest_state is not b._digest_state


async def test_a_trailing_slash_is_the_same_host():
    assert _client(FakeSession())._digest_state is (
        _client(FakeSession(), "10.0.0.1/")._digest_state
    )


# --- the count, which is why the whole state is shared and not just the nonce

async def test_the_count_keeps_rising_across_entries():
    """Digest asks for a strictly increasing nc per nonce. Eleven clients each
    counting from one is exactly what replay protection rejects."""
    session = FakeSession(strict_nc=True)
    clients = [_client(session) for _ in range(11)]

    for c in clients:
        await c.get(URL)

    counts = [nc_of(r) for r in session.requests if nc_of(r)]
    assert len(set(counts)) == len(counts), "the same count was sent twice"
    assert counts == sorted(counts)


async def test_a_strict_device_accepts_every_entry():
    """The end of the same story: no channel is refused for replaying a count."""
    session = FakeSession(strict_nc=True, body="key=value")
    clients = [_client(session) for _ in range(11)]

    results = await asyncio.gather(*(c.get(URL) for c in clients))

    assert all(r == {"key": "value"} for r in results)


# --- recovery ---------------------------------------------------------------

async def test_a_rotated_nonce_is_picked_up_once_for_the_whole_host():
    """When the device moves on, one entry absorbs the stale 401, not eleven."""
    session = FakeSession()
    clients = [_client(session) for _ in range(11)]
    await clients[0].get(URL)

    session.nonce = "nonce-2"
    already_sent = len(session.requests)
    await asyncio.gather(*(c.get(URL) for c in clients))

    after = session.requests[already_sent:]
    stale = [r for r in after
             if "nonce-1" in r["headers"].get("AUTHORIZATION", "")]
    assert len(stale) <= MAX_CONCURRENT_REQUESTS_PER_HOST, (
        "%d of 11 entries each rediscovered the new nonce" % len(stale)
    )
    assert all("nonce-2" in r["headers"].get("AUTHORIZATION", "")
               for r in after[-11:]), "the host did not settle on the new nonce"


async def test_a_wrong_password_still_gives_up():
    """Shared state must not turn one refusal into an endless retry."""
    session = FakeSession(password="not-the-password")
    c = _client(session)

    with pytest.raises(Exception):
        await c.get(URL)
