"""Opening a door over RPC2: which door, and letting go of the session afterwards.

`test_open_door_fallback.py` covers the contract that matters most, which is when the
CGI path may be retried elsewhere and when it must not be: a 404 falls back, an
intermittent 400 on a VTO whose door opened anyway does not, because retrying that could
open the door twice. That is tested against the real `async_access_control_open_door`.

What it substitutes is the RPC2 path itself:

    async def _fallback(door_id):
        return await rpc2.async_open_door(max(0, door_id - 1))
    c._async_open_door_rpc2 = _fallback

which is the right isolation for testing the ordering, but it means the door number
mapping is asserted against a second copy of itself, and the real method's body never
runs. So this file is about the real `_async_open_door_rpc2`.

The mapping is worth pinning precisely because it is **inferred rather than measured**.
The method's own docstring says so: door 1 is channel 0, taken from myhomeiot/DahuaVTO,
whose service defaults to channel 1 and sends 0 on the wire, and consistent with #488
finding door 2 at Index 1. Nobody has confirmed it on hardware. An inference that
nothing pins is one refactor away from being silently replaced by a different guess, and
the thing on the other end is a lock.
"""

import pytest

from custom_components.dahua import client as client_module
from custom_components.dahua.client import DahuaClient


class _Rpc2:
    """Records which channel was opened, and whether the session was released."""

    def __init__(self, result=None, fails=None, logout_raises=None):
        self._result = {"result": True} if result is None else result
        self._fails = fails
        self._logout_raises = logout_raises
        self.opened = []
        self.calls = []

    async def async_open_door(self, channel, door_index=0, short_number="HA"):
        self.opened.append(channel)
        self.calls.append("open")
        if self._fails is not None:
            raise self._fails
        return self._result

    async def logout(self):
        self.calls.append("logout")
        if self._logout_raises is not None:
            raise self._logout_raises
        return True


def _client(monkeypatch, rpc2):
    client = DahuaClient("admin", "pw", "10.0.0.5", 80, 554, object())
    monkeypatch.setattr(client_module, "DahuaRpc2Client", lambda *args, **kwargs: rpc2)
    # The real one builds a session and a connection pool; nothing here uses it.
    monkeypatch.setattr(DahuaClient, "_rpc2_session", lambda self: object())
    return client


# --- which door ---------------------------------------------------------------


async def test_door_one_is_channel_zero(monkeypatch):
    """The inference the docstring records. Pinned so that if anybody ever measures it
    and finds otherwise, the change is deliberate and visible."""
    rpc2 = _Rpc2()
    client = _client(monkeypatch, rpc2)

    await client._async_open_door_rpc2(1)

    assert rpc2.opened == [0]


async def test_door_two_is_channel_one(monkeypatch):
    """Consistent with #488 finding door 2 at Index 1."""
    rpc2 = _Rpc2()
    client = _client(monkeypatch, rpc2)

    await client._async_open_door_rpc2(2)

    assert rpc2.opened == [1]


async def test_door_zero_does_not_become_channel_minus_one(monkeypatch):
    """What the max() is for. A door id of 0 is a caller counting from zero rather than
    one, and a negative channel is not a door on any device."""
    rpc2 = _Rpc2()
    client = _client(monkeypatch, rpc2)

    await client._async_open_door_rpc2(0)

    assert rpc2.opened == [0], "sent a negative channel to a lock"


async def test_the_result_the_device_gave_is_returned(monkeypatch):
    rpc2 = _Rpc2(result={"result": True, "params": {"status": True}})
    client = _client(monkeypatch, rpc2)

    assert await client._async_open_door_rpc2(1) == {
        "result": True,
        "params": {"status": True},
    }


# --- and letting go of the session --------------------------------------------
#
# A private client rather than the shared one, on purpose: this is a one-shot user
# action and must not leave a cached session with a keepalive behind for somebody who
# never enabled RPC2. Which means it has to log out itself, every time.


async def test_it_logs_out_after_opening_the_door(monkeypatch):
    rpc2 = _Rpc2()
    client = _client(monkeypatch, rpc2)

    await client._async_open_door_rpc2(1)

    assert rpc2.calls == ["open", "logout"], rpc2.calls


async def test_it_logs_out_even_when_the_door_refuses(monkeypatch):
    """The reason the logout is in a finally. A door that refuses is exactly when the
    press gets repeated, so a session leaked per attempt accumulates fastest."""
    rpc2 = _Rpc2(fails=RuntimeError("door said no"))
    client = _client(monkeypatch, rpc2)

    with pytest.raises(RuntimeError):
        await client._async_open_door_rpc2(1)

    assert "logout" in rpc2.calls, "the refused attempt kept its session"


async def test_a_logout_failure_does_not_turn_an_opened_door_into_an_error(monkeypatch):
    """An exception raised inside a finally replaces whatever the block was returning.
    Here that would report a failure for a door that opened, and whoever saw it would
    press again."""
    rpc2 = _Rpc2(logout_raises=OSError("connection reset"))
    client = _client(monkeypatch, rpc2)

    assert await client._async_open_door_rpc2(1) == {"result": True}


async def test_a_logout_failure_does_not_hide_why_the_door_did_not_open(monkeypatch):
    """The other direction. The caller turns this into a message for the user, and
    replacing the device's refusal with a connection error from the tidy-up loses the
    only explanation there was."""
    rpc2 = _Rpc2(
        fails=RuntimeError("door said no"), logout_raises=OSError("connection reset")
    )
    client = _client(monkeypatch, rpc2)

    with pytest.raises(RuntimeError, match="door said no"):
        await client._async_open_door_rpc2(1)
