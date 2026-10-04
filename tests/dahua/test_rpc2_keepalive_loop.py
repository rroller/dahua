"""The keepalive that holds one RPC2 login open.

Measured on a DHI-NVR5464-16P-EI: the session survives 90 seconds idle and is gone by
150. A device polled every 150 to 180 seconds, which is exactly what the README tells
people to set to quieten their device's log, would therefore log in on every poll and
lose most of the saving. This loop is what stops that.

`keepalive_needs_starting` already has tests, because the bug it fixes was noticed: a
finished keepalive is not a running one, and the guard used to be `task is None`, so a
returned loop left a completed task in the slot forever and no later read started
another. The loop it guards had no tests at all.

Two things in it are easy to get wrong and quiet when wrong:

  * it returns on a failed ping rather than looping, having dropped the login, so the
    next read logs in again. A loop that kept going would keep pinging a dead session.
  * `asyncio.CancelledError` is re-raised ahead of the broad `except`. If the broad one
    caught it, shutting down would set `holder.task = None` and return normally instead
    of cancelling, so teardown would look like a failed keepalive.
"""

import asyncio

import pytest

from custom_components.dahua.client import _rpc2_keepalive, keepalive_needs_starting


class _Client:
    """Answers global.keepAlive, or fails on the nth ping."""

    def __init__(self, fail_on=None, failure=None):
        self.calls = []
        self._fail_on = fail_on
        self._failure = failure or OSError("connection reset")

    async def request(self, method, params=None, **kwargs):
        self.calls.append((method, params))
        # A loop that has stopped stopping would ping for ever, so it is bounded here.
        # Reaching this is itself the failure, and the count is what the tests assert on.
        if len(self.calls) > 5:
            raise RuntimeError(
                "pinged %d times; the loop is not stopping" % len(self.calls)
            )
        if self._fail_on is not None and len(self.calls) >= self._fail_on:
            raise self._failure
        return {"result": True}


class _Holder:
    def __init__(self, client):
        self.client = client
        self.task = object()
        self.keepalive = None


# --- what it sends ------------------------------------------------------------


async def test_it_asks_the_device_to_hold_the_session_open():
    """`active: False` is a keepalive rather than a command, and the timeout tells the
    device how long to keep the session for, so it has to match the interval this loop
    actually pings at."""
    holder = _Holder(_Client(fail_on=1))

    await _rpc2_keepalive(holder, 0)

    assert holder.client.calls == [
        ("global.keepAlive", {"timeout": 0, "active": False})
    ]


async def test_the_interval_is_not_hardcoded():
    """Small but non-zero, because the loop sleeps the interval before its first ping:
    a realistic 55 would mean waiting 55 seconds to see one."""
    holder = _Holder(_Client(fail_on=1))

    await _rpc2_keepalive(holder, 0.01)

    assert holder.client.calls[0][1]["timeout"] == 0.01


async def test_it_waits_before_the_first_ping():
    """The session has just been created by a read, so pinging immediately would spend a
    request saying nothing. Asserted by starting the loop with an interval nothing in
    the test waits out, rather than by measuring a clock."""
    holder = _Holder(_Client())
    task = asyncio.ensure_future(_rpc2_keepalive(holder, 30))
    try:
        for _ in range(20):
            await asyncio.sleep(0)

        assert holder.client.calls == [], "pinged before waiting the interval"
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


# --- when a ping fails --------------------------------------------------------


async def test_a_failed_ping_drops_the_login():
    """Dropped rather than the holder itself: the entries still hold their references,
    and the next read logs in again."""
    holder = _Holder(_Client(fail_on=1))
    original = holder.task

    await _rpc2_keepalive(holder, 0)

    assert original is not None
    assert holder.task is None


async def test_a_failed_ping_stops_the_loop():
    """It returns rather than carrying on. Continuing would ping a session the device
    has already forgotten, once per interval, for as long as the entry is loaded."""
    client = _Client(fail_on=2)
    holder = _Holder(client)

    await _rpc2_keepalive(holder, 0)

    assert len(client.calls) == 2, "kept pinging after a failure: %d calls" % len(
        client.calls
    )


async def test_it_returns_rather_than_raising_so_nothing_reports_a_crash():
    """The caller runs this as a bare task, so an exception escaping here would surface
    as an unretrieved task exception rather than as the recoverable thing it is."""
    holder = _Holder(_Client(fail_on=1))

    assert await _rpc2_keepalive(holder, 0) is None


async def test_a_dropped_login_is_seen_as_needing_a_new_keepalive():
    """The loop and its guard have to agree, which is the whole of the bug
    keepalive_needs_starting was written for: the loop returns on failure, so the task
    is done rather than absent, and a `task is None` guard would never start another."""
    holder = _Holder(_Client(fail_on=1))
    task = asyncio.ensure_future(_rpc2_keepalive(holder, 0))
    await task
    holder.keepalive = task

    assert keepalive_needs_starting(holder.keepalive) is True


# --- when it is cancelled -----------------------------------------------------


async def test_cancelling_it_does_not_look_like_a_failed_ping():
    """Unloading an entry cancels this task, and that must not drop the login as a side
    effect or look like a device that stopped answering.

    Worth being exact about where the cancellation lands: the loop waits at
    `await asyncio.sleep(interval)`, which is *outside* the try, so a cancellation while
    idle propagates without the handlers seeing it at all. That is the common case and
    it is what this test covers. The `except asyncio.CancelledError: raise` clause exists
    for a cancellation delivered while a ping is in flight, which is the test below.
    """
    holder = _Holder(_Client())
    original = holder.task
    task = asyncio.ensure_future(_rpc2_keepalive(holder, 30))
    for _ in range(20):
        await asyncio.sleep(0)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert holder.task is original, "cancelling dropped the login"
    assert task.cancelled(), "the task finished normally instead of being cancelled"


async def test_a_cancellation_during_a_ping_is_not_swallowed():
    """This is the clause's own case: a cancellation arriving while the ping is in
    flight has to be re-raised rather than caught by the broad `except` below it.

    The ping count is what actually pins it. Asserting only that CancelledError comes
    out is not enough, and finding that out took removing the `raise`: the loop then
    swallows the cancellation and pings again, and whatever eventually stops it raises
    CancelledError from the idle sleep instead, which a `pytest.raises` cannot tell
    apart from the real thing. One ping is the difference.
    """
    client = _Client(fail_on=1, failure=asyncio.CancelledError())
    holder = _Holder(client)

    with pytest.raises(asyncio.CancelledError):
        await _rpc2_keepalive(holder, 0)

    assert (
        len(client.calls) == 1
    ), "the cancellation was swallowed and it pinged again: %d pings" % len(
        client.calls
    )
    assert holder.task is not None, "a cancellation mid-ping dropped the login"
