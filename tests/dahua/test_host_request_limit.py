"""One NVR must not be hit by every channel's config entry at once."""

import asyncio

import pytest

from custom_components.dahua import client as client_module
from custom_components.dahua.client import (
    MAX_CONCURRENT_REQUESTS_PER_HOST,
    DahuaClient,
)


@pytest.fixture(autouse=True)
def _clean_limiters():
    """The limiter registry is module state; keep tests independent."""
    client_module._HOST_LIMITS.clear()
    yield
    client_module._HOST_LIMITS.clear()


class _ConcurrencyProbe:
    """Stands in for the session and records how many calls overlap."""

    def __init__(self, hold=0.02):
        self.hold = hold
        self.current = 0
        self.peak = 0
        self.calls = 0

    async def request(self, method, url, headers=None, **kwargs):
        self.calls += 1
        self.current += 1
        self.peak = max(self.peak, self.current)
        try:
            await asyncio.sleep(self.hold)
        finally:
            self.current -= 1
        return _Resp()


class _Resp:
    status = 200
    headers = {}

    def raise_for_status(self):
        return None

    async def text(self):
        return "key=value"

    async def read(self):
        return b"bytes"

    def close(self):
        return None


def _client(address, session):
    return DahuaClient("u", "p", address, 80, 554, session)


async def test_requests_to_one_host_are_capped():
    probe = _ConcurrencyProbe()
    c = _client("10.0.0.1", probe)

    await asyncio.gather(*(c.get("/cgi-bin/x") for _ in range(20)))

    assert probe.calls == 20, "every request must still be made"
    assert probe.peak <= MAX_CONCURRENT_REQUESTS_PER_HOST, (
        "peak concurrency %d exceeded the cap of %d"
        % (probe.peak, MAX_CONCURRENT_REQUESTS_PER_HOST)
    )


async def test_separate_entries_for_the_same_nvr_share_one_budget():
    """This is the case that wedges the device: one host, many config entries."""
    probe = _ConcurrencyProbe()
    clients = [_client("10.0.0.1", probe) for _ in range(11)]

    await asyncio.gather(*(c.get("/cgi-bin/x") for c in clients for _ in range(3)))

    assert probe.calls == 33
    assert probe.peak <= MAX_CONCURRENT_REQUESTS_PER_HOST


async def test_different_hosts_do_not_share_a_budget():
    """A slow NVR must not throttle an unrelated doorbell."""
    a, b = _ConcurrencyProbe(), _ConcurrencyProbe()
    ca, cb = _client("10.0.0.1", a), _client("10.0.0.2", b)

    assert ca._host_limit is not cb._host_limit

    await asyncio.gather(
        *(ca.get("/cgi-bin/x") for _ in range(6)),
        *(cb.get("/cgi-bin/x") for _ in range(6)),
    )
    assert a.peak <= MAX_CONCURRENT_REQUESTS_PER_HOST
    assert b.peak <= MAX_CONCURRENT_REQUESTS_PER_HOST


async def test_two_clients_same_address_reuse_the_same_semaphore():
    probe = _ConcurrencyProbe()
    assert _client("10.0.0.1", probe)._host_limit is _client("10.0.0.1", probe)._host_limit


async def test_get_bytes_is_capped_too():
    probe = _ConcurrencyProbe()
    c = _client("10.0.0.1", probe)

    await asyncio.gather(*(c.get_bytes("/cgi-bin/snapshot") for _ in range(10)))

    assert probe.calls == 10
    assert probe.peak <= MAX_CONCURRENT_REQUESTS_PER_HOST


async def test_the_event_stream_does_not_hold_a_slot():
    """A long poll under the limiter would starve every other request forever."""
    import inspect

    src = inspect.getsource(DahuaClient.stream_events)
    assert "_host_limit" not in src, "the event stream must not take a request slot"


async def test_waiting_for_a_slot_counts_against_the_timeout():
    """A wedged host should shed load rather than build an unbounded queue."""
    probe = _ConcurrencyProbe(hold=30)
    c = _client("10.0.0.1", probe)

    blockers = [asyncio.create_task(c.get("/cgi-bin/slow"))
                for _ in range(MAX_CONCURRENT_REQUESTS_PER_HOST)]
    await asyncio.sleep(0.05)

    # The queued call must not wait forever; it is bounded by the same timeout.
    queued = asyncio.create_task(c.get("/cgi-bin/queued"))
    await asyncio.sleep(0.05)
    assert not queued.done(), "should still be waiting for a slot"

    for t in blockers + [queued]:
        t.cancel()
    await asyncio.gather(*blockers, queued, return_exceptions=True)
