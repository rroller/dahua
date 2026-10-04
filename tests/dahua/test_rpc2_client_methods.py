"""Logging out, the PTZ object, and opening a door over RPC2.

These are the methods on DahuaRpc2Client that the rest of the RPC2 tests reach through a
fake. Driven directly here, with `request` replaced, because what they do with the
device's answer is the part that had no tests.

Two of them are about letting go of something that belongs to the device rather than to
us, which is why both put it in a `finally`: a PTZ object id and an accessControl object
are the device's, and a doorbell that accumulates them pays for it.
"""

import pytest

from custom_components.dahua.rpc2 import DahuaRpc2Client


def _rpc2(answers=None, session_id="sess-1"):
    """A real client whose `request` answers from a script.

    `answers` maps a method name to either a response dict or an exception to raise.
    Anything not named answers {"result": True}.
    """
    client = DahuaRpc2Client("u", "p", "cam", 80, 554, object())
    client._session_id = session_id
    client._ptz_objects = {}
    client.calls = []
    script = dict(answers or {})

    async def _request(
        method, params=None, object_id=None, verify_result=True, **kwargs
    ):
        client.calls.append(method)
        answer = script.get(method, {"result": True})
        if isinstance(answer, BaseException):
            raise answer
        return answer

    client.request = _request
    return client


# --- logging out --------------------------------------------------------------


async def test_logging_out_without_a_session_asks_the_device_nothing():
    """There is nothing to end, and spending a request saying so would be a line in the
    device's log for no reason."""
    client = _rpc2(session_id=None)
    client._ptz_objects = {0: 42}

    assert await client.logout() is True
    assert client.calls == []
    assert client._ptz_objects == {}


async def test_a_successful_logout_says_so():
    client = _rpc2({"global.logout": {"result": True}})

    assert await client.logout() is True


async def test_a_device_that_refuses_the_logout_is_reported_honestly():
    client = _rpc2({"global.logout": {"result": False}})

    assert await client.logout() is False


async def test_a_logout_that_raises_is_not_an_exception_for_the_caller():
    """Callers log out in a finally, so raising here would replace whatever they were
    actually doing."""
    client = _rpc2({"global.logout": OSError("connection reset")})

    assert await client.logout() is False


async def test_the_session_is_dropped_even_when_the_logout_failed():
    """The important one. Keeping the session id would have the next call reuse a login
    the device may already have ended, and the PTZ object ids are scoped to that session,
    so keeping those addresses objects the device has forgotten."""
    client = _rpc2({"global.logout": OSError("connection reset")})
    client._ptz_objects = {0: 42, 1: 43}

    await client.logout()

    assert client._session_id is None
    assert client._ptz_objects == {}


# --- the PTZ object -----------------------------------------------------------


async def test_the_ptz_object_is_asked_for_once_per_channel():
    """It is session scoped, so asking again per move would be a request per press."""
    client = _rpc2({"ptz.factory.instance": {"result": 7}})

    assert await client.async_get_ptz_object(0) == 7
    assert await client.async_get_ptz_object(0) == 7
    assert client.calls == ["ptz.factory.instance"]


async def test_each_channel_gets_its_own_object():
    client = _rpc2({"ptz.factory.instance": {"result": 7}})

    await client.async_get_ptz_object(0)
    await client.async_get_ptz_object(1)

    assert client.calls == ["ptz.factory.instance", "ptz.factory.instance"]


async def test_it_logs_in_first_when_there_is_no_session():
    client = _rpc2({"ptz.factory.instance": {"result": 7}}, session_id=None)
    logins = []

    async def _login():
        logins.append(1)
        client._session_id = "sess-new"

    client.login = _login

    await client.async_get_ptz_object(0)

    assert logins == [1]


async def test_true_is_not_a_ptz_object():
    """The bool check is load bearing rather than defensive: in Python True is an int and
    is greater than zero, so without it a device answering result=true -- which is what
    something that does not do PTZ answers -- would hand back 1 as an object id, and
    every later call would address object 1."""
    client = _rpc2({"ptz.factory.instance": {"result": True}})

    with pytest.raises(ConnectionError):
        await client.async_get_ptz_object(0)


async def test_a_zero_or_negative_object_is_refused():
    for value in (0, -1):
        client = _rpc2({"ptz.factory.instance": {"result": value}})
        with pytest.raises(ConnectionError):
            await client.async_get_ptz_object(0)


async def test_an_object_that_is_not_a_number_is_refused():
    client = _rpc2({"ptz.factory.instance": {"result": "seven"}})

    with pytest.raises(ConnectionError):
        await client.async_get_ptz_object(0)


async def test_a_refused_object_is_not_remembered():
    """Otherwise the bad answer is cached and every later move uses it."""
    client = _rpc2({"ptz.factory.instance": {"result": 0}})

    with pytest.raises(ConnectionError):
        await client.async_get_ptz_object(0)

    assert client._ptz_objects == {}


# --- opening a door -----------------------------------------------------------


async def test_opening_a_door_is_three_calls_in_order():
    """An object from the factory, the action on that object, and destroy."""
    client = _rpc2({"accessControl.factory.instance": {"result": 9}})

    await client.async_open_door(0)

    assert client.calls == [
        "accessControl.factory.instance",
        "accessControl.openDoor",
        "accessControl.destroy",
    ]


async def test_opening_a_door_logs_in_first_on_a_fresh_client():
    """_async_open_door_rpc2 builds a private client with no session and nothing
    logged it in, so the factory was asked without one. Asked after the login now."""
    client = _rpc2({"accessControl.factory.instance": {"result": 9}}, session_id=None)
    order = []

    async def _login():
        order.append("login")
        client._session_id = "sess-new"

    client.login = _login
    original = client.request

    async def _request(method, *args, **kwargs):
        order.append(method)
        return await original(method, *args, **kwargs)

    client.request = _request

    await client.async_open_door(0)

    assert order[:2] == ["login", "accessControl.factory.instance"]


async def test_opening_a_door_with_a_session_does_not_log_in_again():
    client = _rpc2({"accessControl.factory.instance": {"result": 9}})
    logins = []

    async def _login():
        logins.append(1)

    client.login = _login

    await client.async_open_door(0)

    assert logins == []


async def test_a_factory_that_returns_no_object_stops_before_the_door():
    """Calling openDoor on something that is not an object would either fail obscurely
    or, worse, act on whatever object that id happens to be."""
    client = _rpc2({"accessControl.factory.instance": {"result": True}})

    with pytest.raises(ConnectionError):
        await client.async_open_door(0)

    assert client.calls == ["accessControl.factory.instance"]


async def test_the_object_is_destroyed_even_when_the_door_refuses():
    """The object is the device's, not ours. Leaking one on a doorbell is a real cost,
    and a door that refuses is exactly when the press gets repeated."""
    client = _rpc2(
        {
            "accessControl.factory.instance": {"result": 9},
            "accessControl.openDoor": RuntimeError("door said no"),
        }
    )

    with pytest.raises(RuntimeError):
        await client.async_open_door(0)

    assert "accessControl.destroy" in client.calls


async def test_a_failed_destroy_does_not_lose_the_door():
    """Losing the door's result to a failed tidy-up would be worse than leaking the
    object, so the destroy never raises."""
    client = _rpc2(
        {
            "accessControl.factory.instance": {"result": 9},
            "accessControl.openDoor": {"result": True, "params": {"ok": 1}},
            "accessControl.destroy": OSError("connection reset"),
        }
    )

    assert await client.async_open_door(0) == {"result": True, "params": {"ok": 1}}


async def test_a_failed_destroy_does_not_hide_why_the_door_failed():
    client = _rpc2(
        {
            "accessControl.factory.instance": {"result": 9},
            "accessControl.openDoor": RuntimeError("door said no"),
            "accessControl.destroy": OSError("connection reset"),
        }
    )

    with pytest.raises(RuntimeError, match="door said no"):
        await client.async_open_door(0)


# --- the one-line reads -------------------------------------------------------


async def test_the_current_time_comes_out_of_params():
    client = _rpc2(
        {"global.getCurrentTime": {"params": {"time": "2026-09-29 12:00:00"}}}
    )

    assert await client.current_time() == "2026-09-29 12:00:00"


async def test_the_serial_number_comes_out_of_params():
    client = _rpc2({"magicBox.getSerialNo": {"params": {"sn": "SERIAL1"}}})

    assert await client.get_serial_number() == "SERIAL1"


async def test_the_device_name_comes_from_the_general_table():
    client = _rpc2()

    async def _get_config(params):
        assert params == {"name": "General"}
        return {"table": {"MachineName": "Front Door"}}

    client.get_config = _get_config

    assert await client.get_device_name() == "Front Door"
