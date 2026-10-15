"""A read asked for after its client has started closing.

The other half of #1043's shutdown report, and the case #1064 named as left
over. That one stopped the reads that were already in flight when a client
closed. This one is about the reads that had not gone out yet.

A poll is a fan-out of many reads, and `asyncio.gather` here carries no
`return_exceptions`, which ends the refresh for whoever is awaiting it but
does not cancel the branches still running. So an entry stopping mid-poll
leaves branches that have yet to reach their own read, and each of them
reaches it with the session on its way out:

    RuntimeError: Session is closed
    AssertionError: assert self._connector is not None

The flag is set synchronously at the top of `close()` rather than read off
`session.closed`, because `close()` awaits several times before the
coordinator closes the session. A read issued during those awaits would find
an open session, register itself after the sweep had already passed, and fail
in exactly the reported way.
"""

import asyncio

import pytest

from custom_components.dahua import client as client_module
from custom_components.dahua.client import DahuaClient, ReadAfterClose

MOTION = "/cgi-bin/configManager.cgi?action=getConfig&name=MotionDetect"
LIGHTING = "/cgi-bin/configManager.cgi?action=getConfig&name=Lighting[13][0]"
A_WRITE = "/cgi-bin/configManager.cgi?action=setConfig&Lighting[0][0].Mode=Manual"


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
    """A session that answers, and refuses the way aiohttp does once closed."""

    def __init__(self, hold=0):
        self.hold = hold
        self.closed = False
        self.urls = []

    async def request(self, method, url, headers=None, **kwargs):
        if self.closed:
            # What a request issued on a closed session is answered with, and
            # one of the two lines in the report.
            raise RuntimeError("Session is closed")
        self.urls.append(url)
        await asyncio.sleep(self.hold)
        return _Resp("key=value")


def _client(probe, address="10.0.0.1", port=80, username="u"):
    return DahuaClient(username, "p", address, port, 554, probe)


# --- the read that has not gone out yet -------------------------------------


async def test_a_read_asked_for_after_the_close_never_goes_out():
    """The branch of the poll that had not reached its read yet. Against the
    unfixed client this is the reported `Session is closed`."""
    probe = _Probe()
    client = _client(probe)
    await client.close()
    probe.closed = True  # what the coordinator does next

    with pytest.raises(asyncio.CancelledError):
        await client.get(LIGHTING)

    assert probe.urls == [], "the read went out on a session that was closing"


async def test_a_read_issued_while_the_close_is_still_running_never_goes_out(
    monkeypatch,
):
    """The case a `session.closed` check would miss, and the reason the flag
    is set before the first await.

    `close()` sweeps the reads in flight, releases the RPC2 login and closes
    the RPC2 session, and only then does the coordinator close the session the
    reads use. A read asked for in that window sees an open session and
    registers itself after the sweep has gone past, which is the shape of the
    bug rather than a tidier version of it.
    """
    probe = _Probe()
    client = _client(probe)
    caught = []

    async def _ask_for_a_read_midway(key):
        assert not probe.closed, "the window being modelled is before the close"
        try:
            await client.get(MOTION)
        except BaseException as refusal:  # CancelledError is not an Exception
            caught.append(refusal)

    monkeypatch.setattr(client_module, "_release_rpc2", _ask_for_a_read_midway)
    client._rpc2_acquired = True

    await client.close()

    assert caught and isinstance(caught[0], asyncio.CancelledError)
    assert probe.urls == [], "a read went out while the client was closing"
    assert client_module._HOST_CACHE == {}, (
        "it was registered too, so every other entry on this device would have "
        "waited on it"
    )


async def test_the_refusal_is_not_an_error_the_poll_can_mistake_for_a_verdict():
    """`except Exception` is how the poll records what a device said: a probe
    failure becomes "this capability is absent" and an RPC2 failure can write
    the transport off for the host. Both outlive the entry that is stopping,
    so neither may be told about a read that was never made."""
    client = _client(_Probe())
    await client.close()

    with pytest.raises(ReadAfterClose) as refused:
        await client.get(LIGHTING)

    assert not isinstance(refused.value, Exception)
    assert isinstance(refused.value, asyncio.CancelledError)


# --- what must still happen -------------------------------------------------


async def test_a_write_during_the_close_is_still_attempted():
    """Somebody pressed something. A write is not part of a poll's fan-out,
    and answering it with a cancellation nobody asked for would hide a failure
    that the person who pressed it should see."""
    probe = _Probe()
    client = _client(probe)
    await client.close()

    await client.get(A_WRITE)

    assert probe.urls, "the write was swallowed instead of being sent"


async def test_another_client_on_the_device_still_reads():
    """One entry stopping says nothing about another's poll, and they do not
    share a client. The same scoping #1064 had to be corrected to."""
    mine, theirs = _client(_Probe()), _client(_Probe())

    await mine.close()

    assert await theirs.get(MOTION) == {"key": "value"}
    await theirs.close()


# --- and the logins ---------------------------------------------------------


async def test_a_closing_client_does_not_take_a_new_rpc2_login():
    """`close()` gives this client's reference back, and `_shared_rpc2` builds
    a fresh holder when the host has none. Asking after that point would log
    in again on a device that logs every login (#577), with nobody left to
    release it."""
    client = _client(_Probe())
    client._use_rpc2 = True
    await client.close()

    with pytest.raises(asyncio.CancelledError):
        await client.async_get_product_definition_rpc2()

    assert client_module._HOST_RPC2 == {}


async def test_the_system_load_read_is_refused_too():
    """It does not pass through `get` at all: it is two RPC2 calls with their
    own shared entry, so it needs its own guard."""
    client = _client(_Probe())
    client._use_rpc2 = True
    await client.close()

    with pytest.raises(asyncio.CancelledError):
        await client.async_get_system_load()

    assert client_module._HOST_SYSTEM_LOAD == {}


# --- the ordering this now depends on ---------------------------------------


async def test_the_event_stream_is_handed_over_before_the_client_closes(monkeypatch):
    """The one place the refusal above could do harm, and the reason it does
    not.

    The shared event stream runs on its owner's client, and the RPC2 poller
    asks `_shared_rpc2` at the top of every cycle. host.py's loop treats a
    `CancelledError` as "stop", so a poller left on a closing client would now
    end quietly rather than reconnect, and a surviving entry at the same
    address would lose its events.

    It cannot, because `async_stop` releases the stream first, and releasing
    it hands ownership to a coordinator that is still here. This asserts that
    order rather than restating it in a comment.
    """
    from types import SimpleNamespace

    from custom_components.dahua import coordinator as coordinator_module
    from custom_components.dahua.coordinator import DahuaDataUpdateCoordinator

    order = []

    async def _release(coordinator):
        order.append("the stream was handed over")

    async def _close():
        order.append("the client closed")

    monkeypatch.setattr(coordinator_module, "_release_host_stream", _release)

    coordinator = object.__new__(DahuaDataUpdateCoordinator)
    coordinator._address = "10.0.0.1"
    coordinator.client = SimpleNamespace(close=_close)
    coordinator._session = None
    coordinator._vto_task = None

    await coordinator.async_stop()

    assert order == ["the stream was handed over", "the client closed"]
