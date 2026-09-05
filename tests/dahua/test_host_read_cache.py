"""Eleven channels of one NVR ask the same host-wide questions every poll."""

import asyncio

import pytest

from custom_components.dahua import client as client_module
from custom_components.dahua.client import (
    HOST_CACHE_TTL_SECONDS,
    DahuaClient,
    clear_host_cache,
)

MOTION = "/cgi-bin/configManager.cgi?action=getConfig&name=MotionDetect"
LINKAGE = "/cgi-bin/configManager.cgi?action=getConfig&name=DisableLinkage"
PTZ = "/cgi-bin/ptz.cgi?action=getStatus"
WRITE = "/cgi-bin/configManager.cgi?action=setConfig&MotionDetect[0].Enable=true"


@pytest.fixture(autouse=True)
def _clean_limiters():
    client_module._HOST_LIMITS.clear()
    yield
    client_module._HOST_LIMITS.clear()


class _Probe:
    """Stands in for the session and counts the round trips actually made."""

    def __init__(self, hold=0.02, body="key=value"):
        self.hold = hold
        self.body = body
        self.urls = []

    @property
    def calls(self):
        return len(self.urls)

    async def request(self, method, url, headers=None, **kwargs):
        self.urls.append(url)
        await asyncio.sleep(self.hold)
        return _Resp(self.body)


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


def _client(probe, address="10.0.0.1", username="u"):
    return DahuaClient(username, "p", address, 80, 554, probe)


# --- the case this exists for ----------------------------------------------

async def test_two_entries_asking_at_once_cost_one_round_trip():
    probe = _Probe()
    a, b = _client(probe), _client(probe)

    await asyncio.gather(a.get(MOTION), b.get(MOTION))

    assert probe.calls == 1, "each entry still made its own request"


async def test_eleven_channels_make_one_request_between_them():
    """The NVR shape: one host, one config entry per channel."""
    probe = _Probe()
    clients = [_client(probe) for _ in range(11)]

    results = await asyncio.gather(*(c.get(MOTION) for c in clients))

    assert probe.calls == 1
    assert all(r == {"key": "value"} for r in results), "a waiter got nothing back"


async def test_a_repeat_inside_the_ttl_costs_nothing():
    probe = _Probe(hold=0)
    c = _client(probe)

    await c.get(MOTION)
    await c.get(MOTION)

    assert probe.calls == 1


async def test_different_reads_are_not_confused_for_each_other():
    probe = _Probe(hold=0)
    c = _client(probe)

    await c.get(MOTION)
    await c.get(LINKAGE)

    assert probe.calls == 2


# --- what must never be shared ---------------------------------------------

async def test_a_read_that_names_a_channel_is_not_shared():
    """The key is the URL, so a per-channel read separates itself.

    This is what keeps the cache correct when a hardcoded channel is fixed:
    the URLs stop matching and the sharing stops with them.
    """
    probe = _Probe(hold=0)
    c = _client(probe)

    await c.async_get_config("Lighting[0][0]")
    await c.async_get_config("Lighting[1][0]")

    assert probe.calls == 2, "channel 1 was served channel 0's lighting"


async def test_two_hosts_do_not_share_a_read():
    probe = _Probe(hold=0)
    a, b = _client(probe, "10.0.0.1"), _client(probe, "10.0.0.2")

    await asyncio.gather(a.get(MOTION), b.get(MOTION))

    assert probe.calls == 2


async def test_two_users_on_one_host_do_not_share_a_read():
    """Entries for one NVR can be configured with different credentials."""
    probe = _Probe(hold=0)
    a, b = _client(probe, username="alice"), _client(probe, username="bob")

    await asyncio.gather(a.get(MOTION), b.get(MOTION))

    assert probe.calls == 2


async def test_a_write_is_never_served_from_the_cache():
    probe = _Probe(hold=0)
    c = _client(probe)

    await c.get(WRITE)
    await c.get(WRITE)

    assert probe.calls == 2, "a write was answered without reaching the device"


# --- invalidation -----------------------------------------------------------

async def test_a_write_drops_what_the_host_had_cached():
    """Otherwise a switch would spring back for the rest of the TTL."""
    probe = _Probe(hold=0)
    c = _client(probe)
    await c.get(MOTION)

    await c.get(WRITE)
    await c.get(MOTION)

    assert probe.urls[-1].endswith(MOTION), "the read after the write was stale"
    assert probe.calls == 3


async def test_one_entrys_write_invalidates_for_every_entry_on_the_host():
    probe = _Probe(hold=0)
    a, b = _client(probe), _client(probe)
    await a.get(MOTION)

    await b.get(WRITE)
    await a.get(MOTION)

    assert probe.calls == 3


async def test_a_write_does_not_disturb_another_host():
    probe = _Probe(hold=0)
    a, b = _client(probe, "10.0.0.1"), _client(probe, "10.0.0.2")
    await asyncio.gather(a.get(MOTION), b.get(MOTION))
    before = probe.calls

    await a.get(WRITE)
    await b.get(MOTION)

    assert probe.calls == before + 1, "the other host's read was thrown away too"


async def test_a_write_while_a_read_is_in_flight_does_not_republish_it():
    probe = _Probe(hold=0.05)
    c = _client(probe)
    reading = asyncio.create_task(c.get(MOTION))
    await asyncio.sleep(0)

    await c.get(WRITE)
    await reading

    await c.get(MOTION)
    assert probe.calls == 3, "the in-flight read was published after the write"


async def test_the_last_entry_leaving_forgets_the_host():
    probe = _Probe(hold=0)
    await _client(probe).get(MOTION)
    assert client_module._HOST_CACHE

    clear_host_cache("10.0.0.1")

    assert not client_module._HOST_CACHE


# --- failures ---------------------------------------------------------------

class _FailingProbe(_Probe):
    def __init__(self):
        super().__init__(hold=0)
        self.fail = True

    async def request(self, method, url, headers=None, **kwargs):
        self.urls.append(url)
        if self.fail:
            raise ConnectionError("device is not answering")
        return _Resp(self.body)


async def test_a_failed_read_is_not_remembered():
    """A device that just came back must not be told no for the whole TTL."""
    probe = _FailingProbe()
    c = _client(probe)
    with pytest.raises(Exception):
        await c.get(MOTION)

    probe.fail = False
    assert await c.get(MOTION) == {"key": "value"}
    assert probe.calls == 2


async def test_every_waiter_sees_the_failure():
    probe = _FailingProbe()
    clients = [_client(probe) for _ in range(5)]

    results = await asyncio.gather(
        *(c.get(MOTION) for c in clients), return_exceptions=True
    )

    assert probe.calls == 1
    assert all(isinstance(r, Exception) for r in results)


async def test_one_cancelled_entry_does_not_take_the_read_away():
    """A reload cancels that entry's refresh mid-poll. The others are fine."""
    probe = _Probe(hold=0.05)
    a, b = _client(probe), _client(probe)
    leaving = asyncio.create_task(a.get(MOTION))
    staying = asyncio.create_task(b.get(MOTION))
    await asyncio.sleep(0)

    leaving.cancel()

    assert await staying == {"key": "value"}
    assert probe.calls == 1


# --- shape ------------------------------------------------------------------

async def test_callers_cannot_scribble_on_each_others_result():
    probe = _Probe(hold=0)
    c = _client(probe)

    first = await c.get(MOTION)
    first["key"] = "tampered"
    second = await c.get(MOTION)

    assert second["key"] == "value"


async def test_the_ttl_stays_under_the_shortest_poll_a_user_can_ask_for():
    """A cache that outlives the poll interval would stop the data updating."""
    from custom_components.dahua.const import MIN_SCAN_INTERVAL

    assert 0 < HOST_CACHE_TTL_SECONDS < MIN_SCAN_INTERVAL
