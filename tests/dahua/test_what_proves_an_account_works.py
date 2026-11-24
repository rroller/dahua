"""What counts as the device accepting an account (#1241, second review).

A paused event stream resumes only when the device has accepted the
credentials it attaches with. A poll finishing is not that: rroller ran a
coordinator that finished a refresh having sent the camera nothing, and the
stream attached again on it. Nor is an answer from the read cache, which is
keyed by user and not by password.

So the evidence is taken in the digest exchange: a 2xx to a request that
carried this username and this password. These run the real `DigestAuth` and
the real client against the suite's fake device, which verifies the digest it
is sent.
"""

import aiohttp
import pytest

from custom_components.dahua import client as client_module
from custom_components.dahua.client import (
    DahuaClient,
    credentials_acceptance_mark,
    credentials_accepted_since,
)
from custom_components.dahua.digest import DigestAuth

from .test_digest import PASSWORD, USER, FakeResponse, FakeSession

URL = "/cgi-bin/magicBox.cgi?action=getSystemInfo"


@pytest.fixture(autouse=True)
def _clean():
    client_module._HOST_DIGEST_STATE.clear()
    client_module._CREDENTIALS_ACCEPTED.clear()
    yield
    client_module._HOST_DIGEST_STATE.clear()
    client_module._CREDENTIALS_ACCEPTED.clear()


def _client(session, password=PASSWORD, port=80):
    return DahuaClient(USER, password, "d", port, 554, session)


# --- the exchange -----------------------------------------------------------------


async def _exchange(session, password=PASSWORD, **kwargs):
    heard = []
    auth = DigestAuth(USER, password, session, {})
    auth.on_accepted = lambda: heard.append("accepted")
    response = await auth.request("GET", "http://d/x", **kwargs)
    return response.status, heard


async def test_a_2xx_to_a_signed_request_is_the_device_accepting_the_account():
    status, heard = await _exchange(FakeSession())

    assert status == 200
    assert heard == ["accepted"]


async def test_a_refused_password_is_not():
    status, heard = await _exchange(FakeSession(), password="wrong")

    assert status == 401
    assert heard == []


async def test_a_2xx_to_a_request_that_carried_no_credentials_is_not():
    """A device, or a path, that answers without asking has not been shown a
    password at all."""

    class _AsksNothing:
        def __init__(self):
            self.requests = []

        async def request(self, method, url, headers=None, **kwargs):
            self.requests.append(dict(headers or {}))
            return FakeResponse(200, body="ok=1")

    session = _AsksNothing()
    status, heard = await _exchange(session)

    assert status == 200
    assert "AUTHORIZATION" not in session.requests[0]
    assert heard == []


async def test_a_signed_request_the_device_answered_with_an_error_is_not():
    """The fake checks the signature covers the URI and answers 403 when it
    does not. The password was right; this still is not a 2xx."""
    session = FakeSession(strict_uri=True)
    auth = DigestAuth(USER, PASSWORD, session, {})
    heard = []
    auth.on_accepted = lambda: heard.append("accepted")
    auth._build_digest_header = _signing_another_uri(auth._build_digest_header)

    response = await auth.request("GET", "http://d/x")

    assert response.status == 403
    assert heard == []


def _signing_another_uri(build):
    return lambda method, url: build(method, "http://d/y")


async def test_nobody_listening_is_not_an_error():
    response = await DigestAuth(USER, PASSWORD, FakeSession(), {}).request(
        "GET", "http://d/x"
    )

    assert response.status == 200


# --- through the client -------------------------------------------------------------


async def test_a_read_the_device_answered_is_evidence_for_that_client():
    client = _client(FakeSession(body="deviceType=NVR"))
    mark = credentials_acceptance_mark()

    await client.get(URL)

    assert credentials_accepted_since(client, mark)


async def test_a_read_the_device_refused_is_not():
    session = FakeSession()
    client = _client(session, password="wrong")
    mark = credentials_acceptance_mark()

    # The suite's fake response raises AssertionError for any 4xx, where
    # aiohttp raises ClientResponseError. Either way the read failed.
    with pytest.raises((aiohttp.ClientResponseError, AssertionError)):
        await client.get(URL)
    assert session.requests[-1]["headers"].get(
        "AUTHORIZATION"
    ), "the wrong password was offered, and refused"

    assert not credentials_accepted_since(client, mark)


async def test_an_answer_from_the_cache_is_not_evidence_for_another_password():
    """Two entries, one device, one username, two passwords: the old one in an
    entry not yet updated. The cache is keyed by device, user and URL, so the
    second entry is handed the first one's answer and sends nothing."""
    session = FakeSession(body="deviceType=NVR")
    current = _client(session)
    outdated = _client(session, password="old")
    mark = credentials_acceptance_mark()

    await current.get(URL)
    sent = len(session.requests)
    answer = await outdated.get(URL)

    assert answer, "it was answered"
    assert len(session.requests) == sent, "from the cache: nothing was sent"
    assert credentials_accepted_since(current, mark)
    assert not credentials_accepted_since(outdated, mark)


async def test_an_answer_from_the_cache_is_not_new_evidence_for_the_same_client():
    """What a poll with nothing due looks like from here."""
    client = _client(FakeSession(body="deviceType=NVR"))
    await client.get(URL)
    mark = credentials_acceptance_mark()

    await client.get(URL)

    assert not credentials_accepted_since(client, mark)


async def test_a_still_is_evidence_too():
    """`get_bytes` signs its own request, outside the read cache."""
    client = _client(FakeSession(body="jpeg"))
    mark = credentials_acceptance_mark()

    await client.get_bytes("/cgi-bin/snapshot.cgi?channel=1")

    assert credentials_accepted_since(client, mark)


async def test_another_port_on_the_address_is_another_device():
    session = FakeSession(body="deviceType=NVR")
    one = _client(session)
    other = _client(session, port=81)
    mark = credentials_acceptance_mark()

    await one.get(URL)

    assert credentials_accepted_since(one, mark)
    assert not credentials_accepted_since(other, mark)
