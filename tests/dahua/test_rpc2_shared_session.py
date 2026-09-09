"""One RPC2 login per host, not per config entry.

A Dahua box keeps a finite session table and writes a line to its own log for
every login -- the cost this transport exists to remove. Eleven channels of one
NVR each opening their own session would hand most of the saving back.
"""

import asyncio
from unittest.mock import patch

import pytest

from custom_components.dahua import client as client_module
from custom_components.dahua.client import DahuaClient


class _FakeRpc2:
    """Counts what the device would see."""

    logins = 0
    logouts = 0

    def __init__(self, *args, **kwargs):
        self.session_id = None

    async def login(self):
        type(self).logins += 1
        self.session_id = "s%d" % type(self).logins
        return {"params": {"keepAliveInterval": 60}}

    async def logout(self):
        type(self).logouts += 1
        return True

    async def get_config(self, params):
        return {"table": {"Value": params["name"]}}

    async def request(self, method, params=None, **kwargs):
        return {"result": True}


@pytest.fixture
def hass_session():
    """DahuaClient only stores this; the RPC2 path never touches it."""
    return object()


@pytest.fixture(autouse=True)
def _fresh():
    _FakeRpc2.logins = 0
    _FakeRpc2.logouts = 0
    client_module._HOST_RPC2.clear()
    client_module._HOST_RPC2_UNAVAILABLE.clear()
    yield
    client_module._HOST_RPC2.clear()
    client_module._HOST_RPC2_UNAVAILABLE.clear()


def _client(session, address="10.0.0.1", username="u"):
    return DahuaClient(username, "p", address, 80, 554, session, use_rpc2=True)


async def _read(client, name="General"):
    return await client._rpc2_get_config(name)


@patch.object(client_module, "DahuaRpc2Client", _FakeRpc2)
async def test_every_entry_for_a_host_shares_one_login(hass_session):
    entries = [_client(hass_session) for _ in range(11)]

    await asyncio.gather(*(_read(entry) for entry in entries))

    assert _FakeRpc2.logins == 1, "an NVR's channels each logged in separately"
    key = entries[0]._rpc2_key()
    assert client_module._HOST_RPC2[key].refs == 11


@patch.object(client_module, "DahuaRpc2Client", _FakeRpc2)
async def test_concurrent_first_reads_do_not_race_into_two_logins(hass_session):
    """The login is registered before it is awaited, as _SharedRead does."""
    a, b = _client(hass_session), _client(hass_session)

    await asyncio.gather(_read(a), _read(b))

    assert _FakeRpc2.logins == 1


@patch.object(client_module, "DahuaRpc2Client", _FakeRpc2)
async def test_different_credentials_do_not_share_a_session(hass_session):
    """A session id is obtained with the password and scoped to that user."""
    a = _client(hass_session, username="alice")
    b = _client(hass_session, username="bob")

    await _read(a)
    await _read(b)

    assert _FakeRpc2.logins == 2
    assert len(client_module._HOST_RPC2) == 2


@patch.object(client_module, "DahuaRpc2Client", _FakeRpc2)
async def test_the_session_survives_until_the_last_entry_goes(hass_session):
    a, b = _client(hass_session), _client(hass_session)
    await _read(a)
    await _read(b)
    key = a._rpc2_key()

    await a.close()

    assert key in client_module._HOST_RPC2, "one entry unloading took the session down"
    assert client_module._HOST_RPC2[key].refs == 1
    assert _FakeRpc2.logouts == 0

    await b.close()

    assert key not in client_module._HOST_RPC2
    assert _FakeRpc2.logouts == 1, "the device was left holding the session"


@patch.object(client_module, "DahuaRpc2Client", _FakeRpc2)
async def test_the_keepalive_stops_before_the_session_does(hass_session):
    a = _client(hass_session)
    await _read(a)
    holder = client_module._HOST_RPC2[a._rpc2_key()]
    keepalive = holder.keepalive

    assert keepalive is not None and not keepalive.done()

    await a.close()

    assert keepalive.done(), "the keepalive outlived the session it was holding open"


@patch.object(client_module, "DahuaRpc2Client", _FakeRpc2)
async def test_closing_twice_does_not_release_twice(hass_session):
    """async_stop is reachable three ways; a second release would close the
    session out from under the other entries."""
    a, b = _client(hass_session), _client(hass_session)
    await _read(a)
    await _read(b)

    await a.close()
    await a.close()

    key = b._rpc2_key()
    assert client_module._HOST_RPC2[key].refs == 1, "a double close under-counted"


@patch.object(client_module, "DahuaRpc2Client", _FakeRpc2)
async def test_a_read_after_close_does_not_take_a_new_share(hass_session):
    """Reads should not still be arriving after close, but if one does it must
    not build a session nobody is left to release."""
    a = _client(hass_session)
    await _read(a)
    await a.close()

    assert a._rpc2_released is True
    assert not client_module._HOST_RPC2
