"""A camera should not change identity when its password does.

A device that will not answer `magicBox.cgi` gets an identity synthesised in three places
in client.py:

    md5("{address}_{rtsp_port}_{username}_{password}")

The fallback is what supports those cameras at all. What it is *derived from* is the bug:
change the password, or the RTSP port, or the username, and the same physical camera is a
different device. `_abort_if_unique_id_configured` cannot see the collision, so you get a
second entry with every entity suffixed `_2` and the first one unavailable for ever. #805
is that, and #320 is somebody living it.

**Measured, which is what makes this fixable:** `DHDiscover.search` on UDP 37810 needs no
credentials and returns `SerialNo`. On the recorder here it is byte for byte what
`magicBox.cgi` returns. The reply carries no MAC at all, which is what @rroller asked
about, so the serial is the better answer and it is already extracted by `parse_reply`.

Two limits, both deliberate:

- It is **UDP on the local subnet**, so a camera behind a router answers nothing and keeps
  the hash. Nothing that works today stops working.
- The migration **never merges or deletes**. If the serial is already held by another
  entry then the camera is configured twice, which is #320, and both entries have their
  own entities and history. Naming it and stopping is the only safe automatic move.
"""

import asyncio
from types import SimpleNamespace

import pytest

import custom_components.dahua as dahua
import custom_components.dahua.config_flow as flow_module
import custom_components.dahua.discovery as discovery_module
from custom_components.dahua import (
    async_network_identity,
    is_synthesised_identity,
)
from custom_components.dahua.const import (
    CONF_ADDRESS,
    CONF_CHANNEL,
    CONF_NAME,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_RTSP_PORT,
    CONF_USERNAME,
)

HASH = "4f3a9c8ecafe4f3a9c8ecafe4f3a9c8e"
SERIAL = "BC0A198PAJ779DF"
ADDRESS = "10.0.0.5"


@pytest.fixture(autouse=True)
def _clear_probe_cache():
    """Asked once per host and cached for the process, so it outlives a test too."""
    dahua._HOST_NETWORK_IDENTITY.clear()
    dahua._HOST_NETWORK_IDENTITY_LOCKS.clear()
    yield
    dahua._HOST_NETWORK_IDENTITY.clear()
    dahua._HOST_NETWORK_IDENTITY_LOCKS.clear()


# --- telling a hash from a serial -------------------------------------------

@pytest.mark.parametrize("value", [HASH, HASH + "_1", HASH + "_11"])
def test_a_credentials_hash_is_recognised(value):
    assert is_synthesised_identity(value) is True


@pytest.mark.parametrize("value", [
    SERIAL, SERIAL + "_3", "3C06520PAN00001", "", None, 0,
    HASH[:31], HASH + "abc", HASH.upper(), HASH + "_", HASH + "_x",
])
def test_anything_else_is_left_alone(value):
    """A real serial is shorter, upper case and not hex. `md5().hexdigest()` is lower
    case, so an upper case 32 character string is not one of ours."""
    assert is_synthesised_identity(value) is False


# --- asked once per host ----------------------------------------------------

def _stub_probe(monkeypatch, answer, record=None):
    async def _probe(address):
        if record is not None:
            record.append(address)
        return dict(answer) if answer else {}

    monkeypatch.setattr(discovery_module, "async_probe", _probe)


async def test_the_probe_runs_once_for_a_host(monkeypatch):
    """A recorder has one entry per channel and they all set up at once. Eleven probes
    for one answer would be eleven UDP timeouts on a device that does not reply."""
    calls = []
    _stub_probe(monkeypatch, {"SerialNo": SERIAL}, calls)

    await async_network_identity(SimpleNamespace(), ADDRESS)
    await async_network_identity(SimpleNamespace(), ADDRESS)

    assert calls == [ADDRESS]


async def test_a_silent_device_is_not_asked_again(monkeypatch):
    """The empty answer is cached deliberately: it will be empty for the next channel
    too, and the cost of finding that out is a timeout each time."""
    calls = []
    _stub_probe(monkeypatch, {}, calls)

    await async_network_identity(SimpleNamespace(), ADDRESS)
    await async_network_identity(SimpleNamespace(), ADDRESS)

    assert calls == [ADDRESS]


async def test_two_hosts_are_asked_separately(monkeypatch):
    calls = []
    _stub_probe(monkeypatch, {"SerialNo": SERIAL}, calls)

    await async_network_identity(SimpleNamespace(), ADDRESS)
    await async_network_identity(SimpleNamespace(), "10.0.0.9")

    assert sorted(calls) == ["10.0.0.5", "10.0.0.9"]


async def test_simultaneous_callers_share_one_probe(monkeypatch):
    """Eleven coordinators start together, so the check-then-probe has to be locked or
    they all miss the cache at the same instant."""
    calls = []

    async def _probe(address):
        calls.append(address)
        await asyncio.sleep(0)
        return {"SerialNo": SERIAL}

    monkeypatch.setattr(discovery_module, "async_probe", _probe)

    await asyncio.gather(*[
        async_network_identity(SimpleNamespace(), ADDRESS) for _ in range(5)])

    assert calls == [ADDRESS]


# --- new adds prefer the network serial too --------------------------------

class _Client:
    def __init__(self, serial, *args, **kwargs):
        self._serial = serial
        self.identity_derived_from_credentials = serial == HASH

    async def get_machine_name(self):
        return {"name": "hashy" if self._serial == HASH else "Front Door"}

    async def async_get_system_info(self, strict_auth=False):
        return {"serialNumber": self._serial}


def _flow(monkeypatch, serial, probe_answer, probed=None):
    class _Session:
        async def close(self):
            return None

    monkeypatch.setattr(flow_module, "ClientSession", lambda **kw: _Session())
    monkeypatch.setattr(flow_module, "TCPConnector", lambda **kw: None)
    monkeypatch.setattr(flow_module, "DahuaClient",
                        lambda *a, **kw: _Client(serial))

    async def _probe(address):
        if probed is not None:
            probed.append(address)
        return dict(probe_answer) if probe_answer else {}

    monkeypatch.setattr(flow_module, "async_probe_identity", _probe)
    return flow_module.DahuaFlowHandler()


async def test_a_new_add_prefers_the_network_serial(monkeypatch):
    handler = _flow(monkeypatch, HASH, {"SerialNo": SERIAL})

    data, error = await handler._test_credentials(
        "admin", "pw", ADDRESS, "80", "554", 0)

    assert error is None
    assert data["serialNumber"] == SERIAL, "a new entry took the credentials hash"


async def test_a_device_that_identifies_itself_is_not_probed(monkeypatch):
    probed = []
    handler = _flow(monkeypatch, SERIAL, {"SerialNo": "SOMETHING-ELSE"}, probed)

    data, _error = await handler._test_credentials(
        "admin", "pw", ADDRESS, "80", "554", 0)

    assert probed == [], "it probed a device that answered over HTTP"
    assert data["serialNumber"] == SERIAL


async def test_a_new_add_keeps_the_hash_when_the_probe_is_silent(monkeypatch):
    handler = _flow(monkeypatch, HASH, {})

    data, _error = await handler._test_credentials(
        "admin", "pw", ADDRESS, "80", "554", 0)

    assert data["serialNumber"] == HASH
