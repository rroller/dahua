"""Two Dahua boxes can sit behind one IP on different ports.

A bridge forwarding 80/554 to one camera and 81/555 to another gives two
devices at one address. Everything shared per host was keyed on the address
alone, so the second device was served the first one's answers -- including
`getMachineName` and `getSystemInfo`, which is how Home Assistant works out
what a device is. Reported as #664: the cameras get mixed up after a restart.

Sharing between entries for one real NVR is the point of that cache and must
survive: every entry for one NVR has the same address *and* the same port, so
adding the port to the key changes nothing for them.
"""

import pytest

from custom_components.dahua import client as client_module
from custom_components.dahua.client import DahuaClient, clear_host_cache

MACHINE_NAME = "/cgi-bin/magicBox.cgi?action=getMachineName"
A_WRITE = "/cgi-bin/configManager.cgi?action=setConfig&MotionDetect[0].Enable=true"


class _Resp:
    status = 200
    headers: dict = {}

    def raise_for_status(self):
        return None

    async def text(self):
        return "name=camera"

    def close(self):
        return None


class _Session:
    def __init__(self):
        self.urls = []

    async def request(self, method, url, headers=None, **kwargs):
        self.urls.append(url)
        return _Resp()


@pytest.fixture(autouse=True)
def _clean_shared_state():
    for store in (client_module._HOST_CACHE, client_module._HOST_DIGEST_STATE,
                  client_module._HOST_RPC2, client_module._HOST_LIMITS):
        store.clear()
    client_module._HOST_RPC2_UNAVAILABLE.clear()
    yield
    for store in (client_module._HOST_CACHE, client_module._HOST_DIGEST_STATE,
                  client_module._HOST_RPC2, client_module._HOST_LIMITS):
        store.clear()
    client_module._HOST_RPC2_UNAVAILABLE.clear()


def _client(session, port=80, address="10.0.0.1", user="u"):
    return DahuaClient(user, "p", address, port, 554, session)


async def test_two_devices_on_one_address_do_not_share_a_read():
    """The bug in #664: the second device was answered with the first's name."""
    session = _Session()

    await _client(session, port=80).get(MACHINE_NAME)
    await _client(session, port=81).get(MACHINE_NAME)

    assert len(session.urls) == 2, "the second device was served the first's answer"
    assert session.urls[0] != session.urls[1]


async def test_one_device_still_shares_across_its_entries():
    """Every entry for an NVR has the same address and port. Do not break that."""
    session = _Session()

    await _client(session, port=80).get(MACHINE_NAME)
    await _client(session, port=80).get(MACHINE_NAME)

    assert len(session.urls) == 1, "entries for one NVR stopped sharing a read"


async def test_the_port_is_the_same_device_whether_it_is_text_or_a_number():
    """A config entry stores the port as a string; callers pass an int."""
    session = _Session()

    await _client(session, port="80").get(MACHINE_NAME)
    await _client(session, port=80).get(MACHINE_NAME)

    assert len(session.urls) == 1, "'80' and 80 were treated as two devices"


async def test_a_write_to_one_device_leaves_the_other_cached():
    """A write says the device that took it changed, not its neighbour."""
    session = _Session()
    a, b = _client(session, port=80), _client(session, port=81)
    await a.get(MACHINE_NAME)
    await b.get(MACHINE_NAME)
    assert len(session.urls) == 2

    await a.get(A_WRITE)                      # clears device :80 only

    await b.get(MACHINE_NAME)                 # still cached
    assert len(session.urls) == 3, "the write cleared the other device too"

    await a.get(MACHINE_NAME)                 # must go back to the device
    assert len(session.urls) == 4


async def test_dropping_an_address_drops_every_device_behind_it():
    """The connector teardown has only the address, and takes the lot with it."""
    session = _Session()
    await _client(session, port=80).get(MACHINE_NAME)
    await _client(session, port=81).get(MACHINE_NAME)
    assert len(session.urls) == 2

    clear_host_cache("10.0.0.1")

    await _client(session, port=80).get(MACHINE_NAME)
    await _client(session, port=81).get(MACHINE_NAME)
    assert len(session.urls) == 4


async def test_the_digest_challenge_is_not_carried_between_devices():
    """A nonce is issued by one box and means nothing to another."""
    session = _Session()

    assert _client(session, port=80)._digest_state is not _client(session, port=81)._digest_state
    assert _client(session, port=80)._digest_state is _client(session, port=80)._digest_state


async def test_the_rpc2_session_is_not_carried_between_devices():
    """A session id is scoped to the box that issued it."""
    session = _Session()

    assert _client(session, port=80)._rpc2_key() != _client(session, port=81)._rpc2_key()
    assert _client(session, port=80)._rpc2_key() == _client(session, port=80)._rpc2_key()


async def test_the_request_budget_is_still_shared_by_address():
    """Deliberately not per device.

    The limiter exists because a dozen simultaneous CGI calls wedge Dahua's
    small HTTP server. Two port-mapped devices behind a bridge may be one box,
    so splitting the budget would double the load on the device the reporter is
    already having trouble with.
    """
    session = _Session()

    assert _client(session, port=80)._host_limit is _client(session, port=81)._host_limit
