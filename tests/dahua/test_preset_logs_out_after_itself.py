"""A PTZ preset over RPC2 logs out after itself, whatever happened.

`async_goto_preset_rpc2` builds its own short-lived RPC2 client rather than using the
shared session, calls GotoPreset, and logs out in a `finally`. Every entity that moves a
PTZ camera to a preset goes through it: the camera service and the preset select.

The `finally` is the point. A login that is not released stays on the device until its
own idle timeout, and every login is a line in the device's log -- which this
integration otherwise works quite hard to keep down, to the extent of having a README
section about it. One leaked login per preset press, on a camera somebody is nudging
around from a dashboard, adds up in exactly the place people notice.

It was reached only through mocks before: camera.py and select.py call it, and the tests
for those replace it with an AsyncMock, so the body itself had no coverage at all.
"""

import pytest

from custom_components.dahua import client as client_module
from custom_components.dahua.client import DahuaClient


class _Rpc2:
    """The short-lived RPC2 client, recording the order things happened in."""

    def __init__(self, result=None, fails=None, logout=True, logout_raises=None):
        self._result = {} if result is None else result
        self._fails = fails
        self._logout = logout
        self._logout_raises = logout_raises
        self.calls = []

    async def async_goto_preset_position(self, channel, position):
        self.calls.append(("goto", channel, position))
        if self._fails is not None:
            raise self._fails
        return self._result

    async def logout(self):
        self.calls.append(("logout",))
        if self._logout_raises is not None:
            raise self._logout_raises
        return self._logout


def _client(monkeypatch, rpc2):
    client = DahuaClient("u", "p", "cam", 80, 554, object())

    monkeypatch.setattr(client_module, "DahuaRpc2Client",
                        lambda *args, **kwargs: rpc2)
    # The real one builds a session and a connection pool; nothing here uses it.
    monkeypatch.setattr(DahuaClient, "_rpc2_session", lambda self: object())
    return client


async def test_the_preset_result_is_returned(monkeypatch):
    rpc2 = _Rpc2(result={"result": True})
    client = _client(monkeypatch, rpc2)

    assert await client.async_goto_preset_rpc2(1, 3) == {"result": True}


async def test_the_channel_and_position_are_passed_through(monkeypatch):
    rpc2 = _Rpc2()
    client = _client(monkeypatch, rpc2)

    await client.async_goto_preset_rpc2(2, 7)

    assert ("goto", 2, 7) in rpc2.calls


async def test_it_logs_out_after_a_successful_preset(monkeypatch):
    """Otherwise every press leaves a login on the device until it times out."""
    rpc2 = _Rpc2()
    client = _client(monkeypatch, rpc2)

    await client.async_goto_preset_rpc2(1, 3)

    assert rpc2.calls == [("goto", 1, 3), ("logout",)], rpc2.calls


async def test_it_logs_out_even_when_the_preset_fails(monkeypatch):
    """The whole reason the logout is in a finally. A camera that refuses the move, or
    is too busy to answer, is exactly when a leaked login is least wanted: the press
    will be repeated."""
    rpc2 = _Rpc2(fails=RuntimeError("camera said no"))
    client = _client(monkeypatch, rpc2)

    with pytest.raises(RuntimeError):
        await client.async_goto_preset_rpc2(1, 3)

    assert ("logout",) in rpc2.calls, "the failed preset kept its login"


async def test_it_logs_out_when_the_preset_times_out(monkeypatch):
    """The call is wrapped in a 5 second timeout, and a camera that does not answer is
    the commonest reason a press fails.

    Raised directly rather than driven through a real deadline. `client_module.asyncio`
    *is* the asyncio module, so patching `asyncio.timeout` to fire instantly would be a
    global patch that also caught the logout's own 3 second timeout inside the same
    finally, and the test would then be measuring the patch. TimeoutError is exactly
    what `asyncio.timeout` raises, so this exercises the same path.
    """
    rpc2 = _Rpc2(fails=TimeoutError())
    client = _client(monkeypatch, rpc2)

    with pytest.raises(TimeoutError):
        await client.async_goto_preset_rpc2(1, 3)

    assert ("logout",) in rpc2.calls, "the timed out preset kept its login"


async def test_a_logout_that_reports_failure_is_not_an_error(monkeypatch):
    """The preset worked. Turning the caller's successful press into an exception
    because the tidy-up afterwards was unhappy would be the wrong trade."""
    rpc2 = _Rpc2(result={"result": True}, logout=False)
    client = _client(monkeypatch, rpc2)

    assert await client.async_goto_preset_rpc2(1, 3) == {"result": True}


async def test_a_logout_that_raises_does_not_lose_the_result(monkeypatch):
    """An exception from inside a finally replaces whatever the block was returning,
    so this one has to be swallowed or a working preset reports as broken."""
    rpc2 = _Rpc2(result={"result": True}, logout_raises=OSError("connection reset"))
    client = _client(monkeypatch, rpc2)

    assert await client.async_goto_preset_rpc2(1, 3) == {"result": True}


async def test_a_logout_that_raises_does_not_hide_why_the_preset_failed(monkeypatch):
    """The other half, and the one that matters for diagnosis: an exception from the
    finally would replace the camera's own refusal with a connection error from the
    tidy-up, and the reason the press did not work would be gone."""
    rpc2 = _Rpc2(fails=RuntimeError("camera said no"),
                 logout_raises=OSError("connection reset"))
    client = _client(monkeypatch, rpc2)

    with pytest.raises(RuntimeError, match="camera said no"):
        await client.async_goto_preset_rpc2(1, 3)
