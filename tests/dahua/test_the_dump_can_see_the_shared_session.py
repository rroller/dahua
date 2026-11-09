"""The host block must report the RPC2 state the client actually holds.

Five fields in the diagnostics host block are read out of registries keyed by
the client's own RPC2 key. The block rebuilt that key itself, as
`(address, username)`, and #665 put the port into it
(`_device_key` -> "address:port") without changing the reader. From then on the
lookup could not match, so every dump since has reported:

    rpc2_session_open          False
    rpc2_session_refcount      0
    rpc2_keepalive_running     False
    rpc2_ruled_out_for_host    False
    rpc2_tables_refused        []

for every device, whatever was really going on. Each of those is the healthy
answer, which is why it never looked wrong: a dump taken from a camera whose
session had been ruled out read exactly like a dump from a camera sharing one
happily.

Nothing in the suite registered a holder, so nothing noticed. The one test that
names these fields, test_rpc2_keepalive_restart.py, quotes a dump from 0.9.94
reading `rpc2_session_open` true -- four days before #665, which is consistent:
the field worked then.

Every key here is asked of a real client rather than spelled out, because
spelling it out is the bug.
"""

from types import SimpleNamespace

import pytest

from custom_components.dahua import client as client_module
from custom_components.dahua.client import DahuaClient
from custom_components.dahua.diagnostics import _host_block

ADDRESS = "10.1.2.3"
PORT = 80
USERNAME = "admin"


class _Keepalive:
    """A keepalive task that is still looping."""

    @staticmethod
    def done():
        return False


class _Holder:
    """Stands in for the shared session holder the registry stores."""

    def __init__(self, refs):
        self.refs = refs
        self.keepalive = _Keepalive()


@pytest.fixture
def client():
    return DahuaClient(USERNAME, "password", ADDRESS, PORT, 554, None)


@pytest.fixture(autouse=True)
def _clean_registries():
    client_module._HOST_RPC2.clear()
    client_module._HOST_RPC2_UNAVAILABLE.clear()
    client_module._RPC2_TABLE_UNAVAILABLE.clear()
    yield
    client_module._HOST_RPC2.clear()
    client_module._HOST_RPC2_UNAVAILABLE.clear()
    client_module._RPC2_TABLE_UNAVAILABLE.clear()


def _block(hass, client):
    """The host block for one channel of one entry on this client."""
    coordinator = SimpleNamespace(client=client, _address=ADDRESS)
    config_entry = SimpleNamespace(
        entry_id="entry-1",
        title="Camera",
        data={"address": ADDRESS, "channel": 0},
        state="ConfigEntryState.LOADED",
    )
    return _host_block(hass, coordinator, config_entry)


async def test_an_open_session_is_reported_as_open(hass, client):
    """The field the dump is read for, against the key the client files under."""
    client_module._HOST_RPC2[client._rpc2_key()] = _Holder(refs=4)

    block = _block(hass, client)

    assert block["rpc2_session_open"] is True
    assert block["rpc2_session_refcount"] == 4
    assert block["rpc2_keepalive_running"] is True


async def test_no_session_is_still_reported_as_closed(hass, client):
    """The negative control: an empty registry must not read as a session."""
    block = _block(hass, client)

    assert block["rpc2_session_open"] is False
    assert block["rpc2_session_refcount"] == 0
    assert block["rpc2_keepalive_running"] is False


async def test_a_host_ruled_out_for_rpc2_says_so(hass, client):
    """#1159 was diagnosed against this field reading False on a sick camera."""
    client_module._HOST_RPC2_UNAVAILABLE.add(client._rpc2_key())

    assert _block(hass, client)["rpc2_ruled_out_for_host"] is True


async def test_a_refused_table_is_listed(hass, client):
    client_module._RPC2_TABLE_UNAVAILABLE.add((client._rpc2_key(), "VideoInMode"))

    assert _block(hass, client)["rpc2_tables_refused"] == ["VideoInMode"]


async def test_another_device_on_the_same_address_is_not_borrowed(hass):
    """Two boxes behind one IP on different ports, which is what #665 was for.

    The port belongs in the key, so the dump for the camera on 81 must not
    report the session held by the one on 80. A reader that dropped the port to
    make the lookup match would pass every test above and fail this one.
    """
    on_80 = DahuaClient(USERNAME, "password", ADDRESS, 80, 554, None)
    on_81 = DahuaClient(USERNAME, "password", ADDRESS, 81, 555, None)
    client_module._HOST_RPC2[on_80._rpc2_key()] = _Holder(refs=1)

    assert _block(hass, on_80)["rpc2_session_open"] is True
    assert _block(hass, on_81)["rpc2_session_open"] is False


async def test_a_double_that_is_not_a_client_still_produces_a_block(hass):
    """Most doubles in the suite are not clients, and a dump must not raise.

    They carry `device_key` and no `_rpc2_key`, which is the shape
    tests/dahua/test_diagnostics.py builds.
    """
    double = SimpleNamespace(
        _address=ADDRESS,
        device_key="%s:%d" % (ADDRESS, PORT),
        _username=USERNAME,
        _host_limit=client_module._host_limiter(ADDRESS),
    )
    client_module._HOST_RPC2[(double.device_key, USERNAME)] = _Holder(refs=2)

    block = _block(hass, double)

    assert block["rpc2_session_open"] is True
    assert block["rpc2_session_refcount"] == 2
