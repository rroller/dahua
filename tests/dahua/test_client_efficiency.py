"""The client should not fetch what it does not need, or rebuild what it has."""

import pytest

from custom_components.dahua import client as client_module
from custom_components.dahua.client import DahuaClient


@pytest.fixture(autouse=True)
def _clean_limiters():
    client_module._HOST_LIMITS.clear()
    yield
    client_module._HOST_LIMITS.clear()


class _Resp:
    def __init__(self, recorder):
        self._recorder = recorder
        self.status = 200
        self.headers = {}

    def raise_for_status(self):
        return None

    async def read(self):
        self._recorder["read"] = True
        return b"\xff\xd8" + b"x" * 500_000  # a JPEG we should never pull

    async def text(self):
        self._recorder["read"] = True
        return "key=value"

    def close(self):
        self._recorder["closed"] = True


class _Session:
    def __init__(self, recorder):
        self._recorder = recorder
        self.urls = []

    async def request(self, method, url, headers=None, **kwargs):
        self.urls.append(url)
        return _Resp(self._recorder)


def _client(session):
    return DahuaClient("u", "p", "10.0.0.1", 80, 554, session)


async def test_channel_probe_does_not_download_the_image():
    """It only needs to know the endpoint answers, not what it returns."""
    rec = {}
    session = _Session(rec)

    await _client(session).async_probe_snapshot(0)

    assert session.urls == ["http://10.0.0.1:80/cgi-bin/snapshot.cgi?channel=0"]
    assert rec.get("read") is not True, "the probe pulled the JPEG body"
    assert rec.get("closed") is True, "the response must be released"


async def test_get_bytes_still_reads_the_body():
    """The real snapshot path must be unaffected by the probe's shortcut."""
    rec = {}
    data = await _client(_Session(rec)).get_bytes("/cgi-bin/snapshot.cgi?channel=1")

    assert rec.get("read") is True
    assert data.startswith(b"\xff\xd8")


async def test_rpc2_session_is_built_once_and_reused():
    c = _client(_Session({}))

    first = c._rpc2_session()
    second = c._rpc2_session()

    assert first is second, "a new session per call rebuilds the connection pool"
    await c.close()


async def test_close_releases_the_rpc2_session():
    c = _client(_Session({}))
    session = c._rpc2_session()
    assert not session.closed

    await c.close()

    assert session.closed
    assert c._rpc2_session_instance is None


async def test_close_is_safe_to_call_twice_and_without_use():
    c = _client(_Session({}))
    await c.close()          # never used a session
    await c.close()          # and again

    session = c._rpc2_session()
    await c.close()
    await c.close()
    assert session.closed


async def test_a_closed_session_is_replaced_rather_than_reused():
    c = _client(_Session({}))
    first = c._rpc2_session()
    await c.close()

    second = c._rpc2_session()
    assert second is not first
    assert not second.closed
    await c.close()
