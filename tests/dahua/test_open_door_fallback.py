"""Opening a door on a VTO with no accessControl CGI endpoint.

#465's device answers `404 Not Found` to

    /cgi-bin/accessControl.cgi?action=openDoor&UserID=101&Type=Remote&channel=1

on channel 1 and on channel 0. A 404 means the path does not exist on that
firmware, so no parameter fixes it and the only thing that can help is a second
transport. myhomeiot/DahuaVTO opens the same door over Dahua's JSON protocol.

The ordering is the contract, and most of it is about a lock rather than a
feature:

  * 404/501 -- the endpoint is absent, so try the other route
  * 400     -- #154 reports an intermittent 400 on a VTO whose door opened
               anyway. Retrying that elsewhere could open it twice, so it
               must not fall back. This is the most important test here.
  * 401/403 -- credentials, wrong on any transport
  * both fail -- raise, naming both, because a lock must never fail silently


The suite runs async tests as bare `async def` (asyncio_mode is auto), and the
Home Assistant plugin installs an autouse fixture that needs a running loop --
so `asyncio.run` inside a test errors at setup before reaching the code.
"""

import aiohttp
import pytest

from custom_components.dahua.client import DahuaClient


class _Rpc2:
    """Records the RPC2 conversation."""

    def __init__(self, fail_on=None, object_id=7):
        self.calls = []
        self.fail_on = fail_on
        self.object_id = object_id
        self.logged_out = False

    async def request(self, method, params=None, object_id=None, verify_result=True):
        self.calls.append(method)
        if self.fail_on == method:
            raise ConnectionError("Dahua RPC2 method %s returned result=false" % method)
        if method == "accessControl.factory.instance":
            return {"result": self.object_id}
        return {"result": True}

    async def async_open_door(self, channel, door_index=0, short_number="HA"):
        self.channel = channel
        made = await self.request(
            method="accessControl.factory.instance", params={"channel": channel})
        oid = made.get("result")
        try:
            return await self.request(method="accessControl.openDoor", object_id=oid)
        finally:
            await self.request(method="accessControl.destroy", object_id=oid,
                               verify_result=False)

    async def logout(self):
        self.logged_out = True
        return True


def _client(cgi_status=None, rpc2=None):
    c = DahuaClient("admin", "pw", "10.0.0.5", 80, 554, None, False)
    c.cgi_calls = []

    async def get(url, verify_ok=False):
        c.cgi_calls.append(url)
        if cgi_status is not None:
            raise aiohttp.ClientResponseError(None, None, status=cgi_status, message="x")
        return {"ok": True}

    c.get = get
    if rpc2 is not None:
        async def _fallback(door_id):
            return await rpc2.async_open_door(max(0, door_id - 1))
        c._async_open_door_rpc2 = _fallback
    return c


# --- the ordinary case ------------------------------------------------------

async def test_a_working_cgi_endpoint_is_used_and_nothing_else_is():
    rpc2 = _Rpc2()
    c = _client(cgi_status=None, rpc2=rpc2)

    await c.async_access_control_open_door(1)

    assert len(c.cgi_calls) == 1
    assert rpc2.calls == [], "RPC2 was contacted although CGI worked"


# --- the case this exists for -----------------------------------------------

@pytest.mark.parametrize("status", [404, 501])
async def test_an_absent_endpoint_falls_back(status):
    rpc2 = _Rpc2()
    c = _client(cgi_status=status, rpc2=rpc2)

    await c.async_access_control_open_door(1)

    assert rpc2.calls == ["accessControl.factory.instance",
                          "accessControl.openDoor",
                          "accessControl.destroy"]


async def test_the_object_is_destroyed_even_when_opening_fails():
    """The object belongs to the device, not to us."""
    rpc2 = _Rpc2(fail_on="accessControl.openDoor")
    c = _client(cgi_status=404, rpc2=rpc2)

    with pytest.raises(Exception):
        await c.async_access_control_open_door(1)

    assert "accessControl.destroy" in rpc2.calls


async def test_door_one_is_channel_zero():
    """The CGI path counts doors from 1; the factory counts from 0."""
    rpc2 = _Rpc2()
    c = _client(cgi_status=404, rpc2=rpc2)

    await c.async_access_control_open_door(1)

    assert rpc2.channel == 0


async def test_door_two_is_channel_one():
    rpc2 = _Rpc2()
    c = _client(cgi_status=404, rpc2=rpc2)

    await c.async_access_control_open_door(2)

    assert rpc2.channel == 1


# --- and the ones that must NOT fall back -----------------------------------

async def test_a_400_never_falls_back():
    """#154: a VTO answered 400 while the door opened anyway.

    Retrying that on another transport is how a door gets opened twice, which
    is the one failure here that could actually hurt somebody.
    """
    rpc2 = _Rpc2()
    c = _client(cgi_status=400, rpc2=rpc2)

    with pytest.raises(aiohttp.ClientResponseError):
        await c.async_access_control_open_door(1)

    assert rpc2.calls == [], "a 400 was retried on another transport"


@pytest.mark.parametrize("status", [401, 403, 500])
async def test_other_failures_do_not_fall_back(status):
    """Credentials and server errors are not 'this path does not exist'."""
    rpc2 = _Rpc2()
    c = _client(cgi_status=status, rpc2=rpc2)

    with pytest.raises(aiohttp.ClientResponseError):
        await c.async_access_control_open_door(1)

    assert rpc2.calls == []


async def test_when_both_routes_fail_the_error_names_both():
    """The line a user pastes has to say what was tried."""
    rpc2 = _Rpc2(fail_on="accessControl.factory.instance")
    c = _client(cgi_status=404, rpc2=rpc2)

    with pytest.raises(ConnectionError) as caught:
        await c.async_access_control_open_door(1)

    message = str(caught.value)
    assert "404" in message
    assert "accessControl.factory.instance" in message


def test_the_absent_statuses_are_only_those_two():
    assert DahuaClient.DOOR_CGI_ABSENT == (404, 501)
