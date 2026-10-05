"""One RPC2 login per host, shared, and what happens when it fails.

An NVR is one coordinator per channel and they poll within a moment of each other. If
each opened its own RPC2 session, eleven channels would be eleven logins to a device
that measurably dislikes them, and eleven keepalives holding them open. So the session
is shared per host and user, and the login future is **registered before it is awaited**,
which is what makes channels waking together join the one in flight rather than each
starting another.

None of that was executed by the suite, and every part of it fails quietly. An extra
login is not an error, a keepalive that never starts is not an error, and a leaked
reference is not an error until an entry unloads and the session stays open anyway.

The failure path deserves its own attention. When a login fails the login is cleared and
the **holder is kept**, because the entries still hold references to it and the next read
should try again. Clearing the holder instead would drop those references on the floor.

`client.py` imports no Home Assistant, so these ran locally against the real module
before being committed.
"""

import asyncio
from contextlib import contextmanager

import pytest

from custom_components.dahua import client as client_module
from custom_components.dahua.client import (
    RPC2_KEEPALIVE_FALLBACK_SECONDS,
    RPC2_KEEPALIVE_MARGIN_SECONDS,
    DahuaClient,
)

HOST = "10.0.0.5"
REFUSED = RuntimeError("refused")


class _Rpc2:
    """Stands in for DahuaRpc2Client: it logs in, or it does not."""

    logins = 0
    answer = None

    def __init__(self, *args, **kwargs):
        pass

    async def login(self):
        _Rpc2.logins += 1
        if isinstance(_Rpc2.answer, BaseException):
            raise _Rpc2.answer
        return _Rpc2.answer


@contextmanager
def _rpc2_session(answer=None):
    """Swap in a fake RPC2 client and a keepalive that never runs.

    A context manager rather than a fixture, so that what has been replaced is
    explicit at every call site and so these can be exercised outside pytest.
    `client.py` imports no Home Assistant, and being able to run it directly is
    most of why this module is worth testing this way.

    The keepalive is replaced because the real one is a loop with sleeps in it and
    every test here ends the moment the login resolves. What it was asked for is
    recorded instead, which is the part worth asserting.
    """
    started = []

    def _keepalive(holder, interval):
        # Recorded when it is called, not when the task runs it. The real one is
        # handed to ensure_future, which schedules rather than runs, so a
        # coroutine recording on entry would still be empty at the assertion.
        started.append(interval)

        async def _noop():
            return None

        return _noop()

    real_client = client_module.DahuaRpc2Client
    real_keepalive = client_module._rpc2_keepalive
    _Rpc2.logins = 0
    _Rpc2.answer = {"params": {"keepAliveInterval": 60}} if answer is None else answer
    client_module.DahuaRpc2Client = _Rpc2
    client_module._rpc2_keepalive = _keepalive
    client_module._HOST_RPC2.clear()
    try:
        yield started
    finally:
        client_module.DahuaRpc2Client = real_client
        client_module._rpc2_keepalive = real_keepalive
        client_module._HOST_RPC2.clear()


def _client(address=HOST, username="u"):
    client = object.__new__(DahuaClient)
    client._username = username
    client._password = "p"
    client._address = address
    client._port = 80
    client._rtsp_port = 554
    client._use_https = False
    client._device = "%s:80" % address
    client._rpc2_acquired = False
    client._new_rpc2_session = lambda: object()
    return client


# --- one login for the whole recorder ---------------------------------------


async def test_the_first_read_logs_in():
    with _rpc2_session():
        holder = await _client()._shared_rpc2()

        assert _Rpc2.logins == 1
        assert holder.refs == 1


async def test_every_channel_of_one_recorder_shares_the_login():
    """The point of the whole thing. Eleven channels of an NVR poll together, and a
    device given eleven logins at once is a device that starts refusing."""
    with _rpc2_session():
        channels = [_client() for _ in range(11)]

        holders = [await channel._shared_rpc2() for channel in channels]

        assert _Rpc2.logins == 1
        assert len({id(holder) for holder in holders}) == 1


async def test_channels_waking_together_share_the_login_in_flight():
    """Not merely the finished login. The future is registered before it is
    awaited, so eleven simultaneous first reads join the one already starting
    rather than each starting another."""
    with _rpc2_session():
        channels = [_client() for _ in range(11)]

        await asyncio.gather(*(channel._shared_rpc2() for channel in channels))

        assert _Rpc2.logins == 1


async def test_each_channel_holds_one_reference():
    """The refcount keeps the session open while any entry still wants it, and
    closes it when the last one goes."""
    with _rpc2_session():
        for _ in range(3):
            holder = await _client()._shared_rpc2()

        assert holder.refs == 3


async def test_one_channel_reading_twice_holds_one_reference():
    """Otherwise every poll would add a reference and the session would never be
    released, which is a leak that only shows when an entry is removed."""
    with _rpc2_session():
        channel = _client()

        await channel._shared_rpc2()
        holder = await channel._shared_rpc2()

        assert holder.refs == 1


async def test_two_hosts_do_not_share_a_session():
    with _rpc2_session():
        await _client(address=HOST)._shared_rpc2()
        await _client(address="10.0.0.6")._shared_rpc2()

        assert _Rpc2.logins == 2
        assert len(client_module._HOST_RPC2) == 2


async def test_two_users_on_one_host_do_not_share_a_session():
    """The key is the device and the user. Two entries with different credentials
    on one recorder are two identities to it, so two logins."""
    with _rpc2_session():
        await _client(username="u")._shared_rpc2()
        await _client(username="other")._shared_rpc2()

        assert _Rpc2.logins == 2


# --- when the login fails ---------------------------------------------------


async def test_a_failed_login_is_raised():
    with _rpc2_session(answer=REFUSED):
        with pytest.raises(RuntimeError):
            await _client()._shared_rpc2()


async def test_a_failed_login_is_cleared_so_the_next_read_retries():
    """Left in place, the failed future would be handed to every later read and the
    session would never come back without a restart."""
    with _rpc2_session(answer=REFUSED):
        channel = _client()

        with pytest.raises(RuntimeError):
            await channel._shared_rpc2()

        assert client_module._HOST_RPC2[channel._rpc2_key()].task is None


async def test_a_failed_login_keeps_the_holder():
    """The entries still hold references to it. Dropping the holder would drop
    those, and the refcount is what decides when the session is closed."""
    with _rpc2_session(answer=REFUSED):
        channel = _client()

        with pytest.raises(RuntimeError):
            await channel._shared_rpc2()

        holder = client_module._HOST_RPC2.get(channel._rpc2_key())
        assert holder is not None
        assert holder.refs == 1


async def test_the_retry_after_a_failure_logs_in_again():
    """The whole reason for clearing it. A device that refused once because it was
    rebooting has to come back without Home Assistant restarting."""
    with _rpc2_session(answer=REFUSED):
        channel = _client()
        with pytest.raises(RuntimeError):
            await channel._shared_rpc2()

        _Rpc2.answer = {"params": {"keepAliveInterval": 60}}
        holder = await channel._shared_rpc2()

        assert _Rpc2.logins == 2
        assert holder.task is not None


# --- the keepalive that holds it open ---------------------------------------


async def test_the_keepalive_interval_comes_from_the_device():
    """Minus a margin, so the ping lands before the device's own timeout rather
    than on it."""
    with _rpc2_session({"params": {"keepAliveInterval": 60}}) as started:
        await _client()._shared_rpc2()

        assert started == [60 - RPC2_KEEPALIVE_MARGIN_SECONDS]


async def test_a_device_that_does_not_say_gets_the_fallback():
    with _rpc2_session({"params": {}}) as started:
        await _client()._shared_rpc2()

        assert started == [
            RPC2_KEEPALIVE_FALLBACK_SECONDS - RPC2_KEEPALIVE_MARGIN_SECONDS
        ]


async def test_an_unreadable_interval_gets_the_fallback():
    """A word where a number should be. Letting it through would raise inside the
    login path, which is a working session lost to a cosmetic answer."""
    with _rpc2_session({"params": {"keepAliveInterval": "soon"}}) as started:
        await _client()._shared_rpc2()

        assert started == [
            RPC2_KEEPALIVE_FALLBACK_SECONDS - RPC2_KEEPALIVE_MARGIN_SECONDS
        ]


async def test_a_very_short_interval_is_floored():
    """A device claiming a six second timeout would otherwise be pinged every
    second, which is worse than the idle logout it is avoiding."""
    with _rpc2_session({"params": {"keepAliveInterval": 6}}) as started:
        await _client()._shared_rpc2()

        assert started == [5.0]


async def test_the_keepalive_is_started_once_for_the_recorder():
    """Eleven channels, one session, one keepalive. One per channel would be eleven
    pings per interval arriving at the device."""
    with _rpc2_session() as started:
        for _ in range(11):
            await _client()._shared_rpc2()

        assert len(started) == 1
