"""Ringing a room from a VTO: the `vto_call` service.

The integration could cancel a VTO call and had nothing to start one. The request
sequence here was read out of the web interface of a DHI-VTO2211G-WP-S2 on
4.810.0000000.0.R (the phone icon under Device Setting) and replayed against it:

    VideoTalkPhone.factory.instance   params null, result is the object
    VideoTalkPhone.beginCall          on that object,
                                      {"number": "9901", "type": "normal", "isTestCall": true}

With that, the room's main monitor and both its extensions (VTH2421F-P, 4.800) rang
and showed the VTO's camera, as a press of the button does. Logging out straight
after beginCall did not end the call, and the object id came back as an integer.

Pinned here because a device answers a well-formed request it does not understand
with a plausible reply: a wrong method name or a missing `isTestCall` is not going to
show up as an error anywhere else.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.dahua import client as client_module
from custom_components.dahua.camera import DahuaCamera
from custom_components.dahua.client import DahuaClient, vto_call_number
from custom_components.dahua.rpc2 import DahuaRpc2Client, Rpc2MethodRefused


def _rpc2(answers=None, session_id="sess-1"):
    """A real client whose `request` answers from a script and records every request.

    `answers` maps a method name to a response dict or an exception to raise.
    Anything not named answers {"result": True}.
    """
    client = DahuaRpc2Client("u", "p", "vto", 80, 554, object())
    client._session_id = session_id
    client.sent = []
    client.logins = 0
    script = dict(answers or {})

    async def _request(method, params="unset", object_id=None, verify_result=True,
                       **kwargs):
        client.sent.append({"method": method, "params": params, "object": object_id})
        answer = script.get(method, {"result": True})
        if isinstance(answer, BaseException):
            raise answer
        return answer

    async def _login():
        client.logins += 1
        client._session_id = "sess-new"

    client.request = _request
    client.login = _login
    return client


def _methods(client):
    return [sent["method"] for sent in client.sent]


# --- the requests -------------------------------------------------------------

async def test_a_call_is_an_object_then_begin_call_on_it():
    """The exact sequence the VTO's own page sends, down to the params."""
    client = _rpc2({"VideoTalkPhone.factory.instance": {"result": 27581624}})

    await client.async_vto_call("9901")

    assert client.sent == [
        {"method": "VideoTalkPhone.factory.instance", "params": None, "object": None},
        {"method": "VideoTalkPhone.beginCall",
         "params": {"number": "9901", "type": "normal", "isTestCall": True},
         "object": 27581624},
    ]


async def test_the_factory_is_asked_with_params_null_not_without_params():
    """The page sends `"params": null`. Leaving params out is a different request,
    and `request` only omits the key when nothing at all is passed."""
    client = _rpc2({"VideoTalkPhone.factory.instance": {"result": 5}})

    await client.async_vto_call("9901")

    assert client.sent[0]["params"] is None


async def test_the_number_is_sent_as_given():
    """Turning a room into a number is the client's job, and done once. A second
    rewrite down here would make `9901#10` and `9901#1` easy to confuse."""
    client = _rpc2({"VideoTalkPhone.factory.instance": {"result": 5}})

    await client.async_vto_call("9901#1")

    assert client.sent[1]["params"]["number"] == "9901#1"


async def test_it_logs_in_first_when_there_is_no_session():
    """The client calls this on a fresh, private client, which has no session."""
    client = _rpc2({"VideoTalkPhone.factory.instance": {"result": 5}}, session_id=None)

    await client.async_vto_call("9901")

    assert client.logins == 1
    assert _methods(client)[0] == "VideoTalkPhone.factory.instance"


async def test_an_existing_session_is_not_logged_in_again():
    client = _rpc2({"VideoTalkPhone.factory.instance": {"result": 5}})

    await client.async_vto_call("9901")

    assert client.logins == 0


async def test_nothing_ends_or_destroys_the_call_it_just_started():
    """The page destroys the object only after endCall, once the call is over.
    Doing either here could hang up the call this exists to make."""
    client = _rpc2({"VideoTalkPhone.factory.instance": {"result": 5}})

    await client.async_vto_call("9901")

    assert "VideoTalkPhone.endCall" not in _methods(client)
    assert "VideoTalkPhone.destroy" not in _methods(client)


@pytest.mark.parametrize("bad", [True, False, 0, -1, None, "5", 5.0])
async def test_a_factory_that_returns_no_object_stops_before_the_call(bad):
    """beginCall on something that is not an object would fail obscurely, or act on
    whatever that id happens to be. True is listed because bool is an int."""
    client = _rpc2({"VideoTalkPhone.factory.instance": {"result": bad}})

    with pytest.raises(ConnectionError):
        await client.async_vto_call("9901")

    assert _methods(client) == ["VideoTalkPhone.factory.instance"]


async def test_a_refused_call_is_raised_as_a_refusal():
    """So the service can tell the user the device's reason."""
    client = _rpc2({
        "VideoTalkPhone.factory.instance": {"result": 5},
        "VideoTalkPhone.beginCall": Rpc2MethodRefused("refused", code=268632085),
    })

    with pytest.raises(Rpc2MethodRefused):
        await client.async_vto_call("9901")


# --- the room number ----------------------------------------------------------

@pytest.mark.parametrize("room, number", [
    ("9901#0", "9901"),       # the main monitor, as a VTH shows its own room
    (" 9901#0 ", "9901"),     # pasted with spaces
    ("9901", "9901"),         # already a number
    ("9901#1", "9901#1"),     # an extension keeps its suffix
    ("9901#10", "9901#10"),   # a different room, not 9901#1 and not 9901
    ("9901#00", "9901#00"),   # not the measured shape; left alone
    ("1#1#8001#100", "1#1#8001#100"),  # apartment form, not measured
    ("1#1#8001#0", "1#1#8001#0"),      # nor this one
])
def test_only_a_bare_number_with_hash_zero_loses_the_suffix(room, number):
    assert vto_call_number(room) == number


# --- the client: one session, given back ----------------------------------------

class _FakeRpc2:
    def __init__(self, fails=None, logout_raises=None):
        self._fails = fails
        self._logout_raises = logout_raises
        self.numbers = []
        self.calls = []

    async def async_vto_call(self, number):
        self.numbers.append(number)
        self.calls.append("call")
        if self._fails is not None:
            raise self._fails
        return {"result": True}

    async def logout(self):
        self.calls.append("logout")
        if self._logout_raises is not None:
            raise self._logout_raises
        return True


def _client(monkeypatch, rpc2):
    client = DahuaClient("admin", "pw", "10.0.0.5", 80, 554, object())
    monkeypatch.setattr(client_module, "DahuaRpc2Client", lambda *args, **kwargs: rpc2)
    monkeypatch.setattr(DahuaClient, "_rpc2_session", lambda self: object())
    return client


async def test_the_client_dials_the_number_not_the_room(monkeypatch):
    rpc2 = _FakeRpc2()
    client = _client(monkeypatch, rpc2)

    await client.async_vto_call("9901#0")

    assert rpc2.numbers == ["9901"]


async def test_it_logs_out_after_starting_the_call(monkeypatch):
    """Measured: the room kept ringing after global.logout, so there is no reason to
    hold a session for the length of the call."""
    rpc2 = _FakeRpc2()
    client = _client(monkeypatch, rpc2)

    await client.async_vto_call("9901")

    assert rpc2.calls == ["call", "logout"]


async def test_it_logs_out_even_when_the_call_is_refused(monkeypatch):
    rpc2 = _FakeRpc2(fails=Rpc2MethodRefused("refused"))
    client = _client(monkeypatch, rpc2)

    with pytest.raises(Rpc2MethodRefused):
        await client.async_vto_call("9901")

    assert rpc2.calls == ["call", "logout"]


async def test_a_logout_failure_does_not_turn_a_started_call_into_an_error(monkeypatch):
    """Reporting a failure for a room that is ringing gets the button pressed again."""
    rpc2 = _FakeRpc2(logout_raises=OSError("connection reset"))
    client = _client(monkeypatch, rpc2)

    assert await client.async_vto_call("9901") == {"result": True}


async def test_a_logout_failure_does_not_hide_why_the_call_was_refused(monkeypatch):
    rpc2 = _FakeRpc2(fails=Rpc2MethodRefused("room unknown"),
                     logout_raises=OSError("connection reset"))
    client = _client(monkeypatch, rpc2)

    with pytest.raises(Rpc2MethodRefused, match="room unknown"):
        await client.async_vto_call("9901")


# --- the service handler ------------------------------------------------------

def _camera(*, doorbell=True, fails=None):
    camera = object.__new__(DahuaCamera)
    camera._logical_channel = 0
    camera._coordinator = SimpleNamespace(
        client=SimpleNamespace(async_vto_call=AsyncMock(side_effect=fails)),
        is_doorbell=lambda: doorbell,
        get_device_name=lambda: "Front Door",
    )
    return camera, camera._coordinator.client


async def test_calling_from_a_camera_that_is_not_a_doorbell_says_so():
    camera, client = _camera(doorbell=False)

    with pytest.raises(HomeAssistantError) as err:
        await camera.async_vto_call("9901")

    assert err.value.translation_key == "vto_call_needs_a_doorbell"
    assert err.value.translation_placeholders == {"device": "Front Door"}
    client.async_vto_call.assert_not_awaited()


async def test_a_doorbell_passes_the_room_on():
    camera, client = _camera()

    await camera.async_vto_call("9901#0")

    client.async_vto_call.assert_awaited_once_with("9901#0")


async def test_a_refusal_reaches_the_user_with_the_room_and_the_reason():
    camera, _ = _camera(fails=Rpc2MethodRefused(
        "Dahua RPC2 method VideoTalkPhone.beginCall returned result=false (code=1)"))

    with pytest.raises(HomeAssistantError) as err:
        await camera.async_vto_call("9902")

    assert err.value.translation_key == "vto_call_refused"
    assert err.value.translation_placeholders["room"] == "9902"
    assert "code=1" in err.value.translation_placeholders["reason"]


async def test_a_connection_failure_is_not_reported_as_a_refusal():
    """A refusal is the device answering no. An unreachable VTO is not that, and
    saying the doorbell would not call the room would send the user looking at
    the wrong thing."""
    camera, _ = _camera(fails=ConnectionError("unreachable"))

    with pytest.raises(ConnectionError):
        await camera.async_vto_call("9901")
