"""One NVR must not be hit by every channel's config entry at once."""

import asyncio

import pytest

from custom_components.dahua import client as client_module
from custom_components.dahua.client import (
    MAX_CONCURRENT_REQUESTS_PER_HOST,
    MAX_QUEUE_WAIT_SECONDS,
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
    assert (
        probe.peak <= MAX_CONCURRENT_REQUESTS_PER_HOST
    ), "peak concurrency %d exceeded the cap of %d" % (
        probe.peak,
        MAX_CONCURRENT_REQUESTS_PER_HOST,
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
    assert (
        _client("10.0.0.1", probe)._host_limit is _client("10.0.0.1", probe)._host_limit
    )


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
    for spelling in ("_host_limit", "_request_slot"):
        assert spelling not in src, (
            "the event stream must not take a request slot (found %s)" % spelling
        )


async def test_a_request_waits_for_a_slot_rather_than_overrunning_the_cap():
    """The queue is real: a call arriving with no slot free does not proceed."""
    probe = _ConcurrencyProbe(hold=30)
    c = _client("10.0.0.1", probe)

    blockers = [
        asyncio.create_task(c.get("/cgi-bin/slow"))
        for _ in range(MAX_CONCURRENT_REQUESTS_PER_HOST)
    ]
    await asyncio.sleep(0.05)

    queued = asyncio.create_task(c.get("/cgi-bin/queued"))
    await asyncio.sleep(0.05)
    assert not queued.done(), "should still be waiting for a slot"

    for t in blockers + [queued]:
        t.cancel()
    await asyncio.gather(*blockers, queued, return_exceptions=True)


async def test_a_queued_request_is_not_charged_to_the_device(monkeypatch):
    """#1043: the cap is ours, so waiting for it must not accuse the device.

    The device here answers every single request, and promptly. What it cannot
    do is answer them faster than this integration is willing to send them, and
    a recorder with nineteen channels queues several hundred reads behind two
    slots. While the device's timeout also measured the wait, the back of that
    queue expired without ever being sent, and the log blamed a device that had
    never been asked: one channel of nineteen set up.
    """
    monkeypatch.setattr(client_module, "TIMEOUT_SECONDS", 0.5)
    probe = _ConcurrencyProbe(hold=0.2)
    c = _client("10.0.0.1", probe)

    # Ten reads at two at a time is 1.0s of queue against a 0.5s timeout, so
    # the back half expires while waiting if the wait is on the device's clock.
    #
    # Few and slow rather than many and fast, which is how this was first
    # written: sixty reads holding 0.02s each against a 0.2s timeout is the
    # same 5x ratio on paper and is not the same test. Scheduling sixty
    # coroutines costs time inside each one's own timeout, measured here at up
    # to 0.25s of a 0.2s budget, so the test failed on a loaded machine for a
    # reason that had nothing to do with the queue. The ratio that matters is
    # the one between a single read and the timeout, and ten reads keep it at
    # 2x where sixty left it below 1.
    results = await asyncio.gather(
        *(c.get("/cgi-bin/x") for _ in range(10)), return_exceptions=True
    )

    expired = [r for r in results if isinstance(r, asyncio.TimeoutError)]
    assert not expired, "%d of 10 expired while queued, having never been sent" % len(
        expired
    )
    assert probe.calls == 10, "every request must reach the device"


async def test_the_wait_for_a_slot_has_a_ceiling_of_its_own(monkeypatch):
    """A device that never answers must not stretch one fan-out indefinitely.

    Each slot is held for the full device timeout, and the queue is only two
    wide, so without a ceiling the fan-out grows with its own depth: measured
    at 50s for a hundred reads on the harness in #1043. The ceiling bounds it
    at roughly MAX_QUEUE_WAIT_SECONDS however deep the queue is.
    """
    monkeypatch.setattr(client_module, "TIMEOUT_SECONDS", 0.1)
    monkeypatch.setattr(client_module, "MAX_QUEUE_WAIT_SECONDS", 0.3)
    probe = _ConcurrencyProbe(hold=30)
    c = _client("10.0.0.1", probe)

    started = asyncio.get_running_loop().time()
    results = await asyncio.gather(
        *(c.get("/cgi-bin/x") for _ in range(40)), return_exceptions=True
    )
    elapsed = asyncio.get_running_loop().time() - started

    assert all(isinstance(r, asyncio.TimeoutError) for r in results)
    # What the ceiling does, counted rather than timed. A request that waited
    # it out gives up without being sent, so with slots freeing every 0.1s only
    # the first few waves ever reach the device: measured at 8 of 40, against
    # 40 of 40 with the ceiling lifted. The clock below says the same thing and
    # says it less reliably, because the 40 expiries cost real time of their
    # own -- each logs a warning -- and that cost is not what is being claimed.
    assert probe.calls <= 12, (
        "%d of 40 reached the device, so the queue was not bounded" % probe.calls
    )
    # Ungoverned this is 40 / 2 * 0.1 = 2.0s and grows with every extra read.
    # Measured at 0.7s with the ceiling and 2.3s without it on the same box.
    assert elapsed < 1.6, (
        "the fan-out took %.1fs, so the ceiling did not hold" % elapsed
    )


def test_the_slot_is_taken_in_one_place_only():
    """Nothing may reach for the semaphore and start a clock in the wrong order.

    Derived from the source rather than restated: the ordering bug in #1043 was
    one `async with` per call site, eight of them, each free to get it wrong.
    There is now a single helper that takes a slot, and this is what keeps it
    single.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(client_module))
    allowed = {"__init__", "_request_slot"}
    offenders = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name in allowed:
            continue
        for inner in ast.walk(node):
            if (
                isinstance(inner, ast.Attribute)
                and inner.attr == "_host_limit"
                and isinstance(inner.value, ast.Name)
                and inner.value.id == "self"
            ):
                offenders.add(node.name)

    assert not offenders, (
        "these take the request slot directly instead of through "
        "_request_slot(), which is where the wait is bounded: %s" % sorted(offenders)
    )


def test_the_ceiling_is_in_units_of_the_device_s_patience():
    """A ceiling only bounds anything while it is a small multiple of the wait.

    The test above patches the ceiling, so it proves the mechanism and not the
    number. This holds the number: long enough that a queue may legitimately
    form -- the deepest poll measured drains in 26s -- and short enough that a
    device which never answers cannot stretch one fan-out indefinitely, which
    is the failure mode the old ordering was there to prevent.
    """
    assert MAX_QUEUE_WAIT_SECONDS >= client_module.TIMEOUT_SECONDS, (
        "a request must be allowed to queue for longer than a single read "
        "takes, or a deep fan-out sheds load the device could have served"
    )
    assert MAX_QUEUE_WAIT_SECONDS <= 4 * client_module.TIMEOUT_SECONDS, (
        "a wedged device holds every slot for TIMEOUT_SECONDS, so a ceiling "
        "this far above it stops bounding the fan-out at all"
    )
