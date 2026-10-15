"""A read still in flight when its session closes.

Reported in #1043 on a Home Assistant restart. Stopping an entry closes the
session the shared reads are using, and the reads were only forgotten, not
stopped, so they carried on against a session that was no longer there:

    WARNING Exception fetching information from
      http://192.168.68.105:80/cgi-bin/configManager.cgi?action=getConfig&name=Lighting[13][0]
    ERROR Error doing job: AssertionError exception in shielded future (task: None)
      File "custom_components/dahua/digest.py", line 170, in _send
        return await self.session.request(method, url, headers=headers, **kwargs)
        assert self._connector is not None
    RuntimeError: Session is closed

Nothing is lost by stopping them: the entry is going away and the answer has
nowhere to arrive. What matters is the line between the two cases, because a
write forgets a shared read as well and there the read must survive, since
whoever is waiting on it still wants the answer.
"""

import asyncio

import pytest

from custom_components.dahua import client as client_module
from custom_components.dahua.client import DahuaClient, clear_host_cache

MOTION = "/cgi-bin/configManager.cgi?action=getConfig&name=MotionDetect"
LIGHTING = "/cgi-bin/configManager.cgi?action=getConfig&name=Lighting[13][0]"


@pytest.fixture(autouse=True)
def _clean_limiters():
    client_module._HOST_LIMITS.clear()
    yield
    client_module._HOST_LIMITS.clear()


class _Resp:
    status = 200
    headers = {}

    def __init__(self, body):
        self._body = body

    def raise_for_status(self):
        return None

    async def text(self):
        return self._body

    def close(self):
        return None


class _Probe:
    """A session that answers slowly, and like aiohttp once it is closed."""

    def __init__(self, hold=5):
        self.hold = hold
        self.closed = False
        self.urls = []

    async def request(self, method, url, headers=None, **kwargs):
        self.urls.append(url)
        await asyncio.sleep(self.hold)
        if self.closed:
            # What aiohttp does to a request that outlives its session, which
            # is the failure in the report: an assertion rather than an
            # ordinary error, raised where nobody is waiting to see it.
            raise AssertionError("assert self._connector is not None")
        return _Resp("key=value")


def _client(probe, address="10.0.0.1", port=80, username="u"):
    return DahuaClient(username, "p", address, port, 554, probe)


async def _started(task):
    """Let the read reach the probe, so it is genuinely in flight."""
    for _ in range(50):
        await asyncio.sleep(0)
        if not task.done():
            return
    raise AssertionError("the read never got going")


def _cached(device):
    return [key for key in client_module._HOST_CACHE if key[0] == device]


# --- shutdown ---------------------------------------------------------------


async def test_a_read_in_flight_is_stopped_when_the_client_closes():
    """The probe answers after the close here, the way the device did on the
    install that reported this: the read was still out when the session went,
    and what it raised had nowhere to go."""
    probe = _Probe(hold=0.05)
    client = _client(probe)
    reader = asyncio.ensure_future(client.get(LIGHTING))
    await _started(reader)
    in_flight = client_module._HOST_CACHE[("10.0.0.1:80", "u", LIGHTING)].task

    await client.close()
    probe.closed = True  # what _close_session does to the session next
    await asyncio.sleep(0.2)  # long enough for the read to come back into it

    assert in_flight.cancelled(), (
        "the read outlived the session it was issued on, which is what raised "
        "AssertionError with nobody waiting for it"
    )
    with pytest.raises(asyncio.CancelledError):
        await reader


async def test_a_cancelled_read_is_not_left_in_the_cache():
    """Otherwise the next client on this device waits on a dead task."""
    probe = _Probe()
    client = _client(probe)
    reader = asyncio.ensure_future(client.get(MOTION))
    await _started(reader)

    await client.close()

    assert _cached("10.0.0.1:80") == []
    with pytest.raises(asyncio.CancelledError):
        await reader


async def test_what_the_device_already_answered_is_left_alone():
    """A read that has landed does not depend on any session, and another
    entry on this device may be about to be told the same thing. Dropping it
    would spend a request on the hardware to be told it twice."""
    probe = _Probe(hold=0)
    client = _client(probe)
    await client.get(MOTION)

    await client.close()

    assert _cached("10.0.0.1:80") != []


async def test_only_this_devices_reads_are_stopped():
    """Two boxes behind one address are not each other (#664), and one of them
    stopping must not take the other's poll with it."""
    mine, theirs = _client(_Probe(), port=80), _client(_Probe(), port=8080)
    ours = asyncio.ensure_future(mine.get(MOTION))
    yours = asyncio.ensure_future(theirs.get(MOTION))
    await _started(ours)
    await _started(yours)

    await mine.close()

    assert not yours.done(), "the other device's read was taken away"
    assert _cached("10.0.0.1:8080") != []

    await theirs.close()
    await asyncio.gather(ours, yours, return_exceptions=True)


# --- and the case that must keep working ------------------------------------


async def test_a_write_still_leaves_an_unfinished_read_alone():
    """A write drops the shared read so the next poll asks again. It does not
    stop the read, because whoever is waiting on it still wants the answer."""
    probe = _Probe(hold=0.05)
    client = _client(probe)
    reader = asyncio.ensure_future(client.get(MOTION))
    await _started(reader)

    clear_host_cache("10.0.0.1:80")

    assert await reader == {"key": "value"}


async def test_another_entry_on_the_same_device_keeps_its_read():
    """One read of one URL is shared by every entry on the device, and they do
    not share a session: there is one per channel, closed by its own
    coordinator. So "the reads on this device" is the wrong set to stop. The
    right one is the reads issued on the session that is closing, or unloading
    one entry takes away a poll that is still running in another."""
    mine, theirs = _client(_Probe()), _client(_Probe(hold=0.05))
    read = asyncio.ensure_future(theirs.get(MOTION))
    await _started(read)

    await mine.close()

    assert await read == {"key": "value"}
    await theirs.close()
