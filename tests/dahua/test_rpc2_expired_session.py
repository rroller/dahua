"""An expired direct-camera RPC2 login is renewed for one retry."""

from unittest.mock import AsyncMock, patch

import aiohttp
import pytest

from custom_components.dahua import client as client_module
from custom_components.dahua.client import DahuaClient
from custom_components.dahua.rpc2 import DahuaRpc2Client, Rpc2MethodRefused

EXPIRED = Rpc2MethodRefused(
    "expired", code=287637504, message="session is out of date!"
)


class FakeRpc2:
    logins = 0
    failures = []
    calls = []

    def __init__(self, *args):
        self._session_id = None
        self._ptz_objects = {}

    async def login(self):
        type(self).logins += 1
        self._session_id = "session-%d" % type(self).logins
        return {"params": {"keepAliveInterval": 60}}

    async def logout(self):
        return True

    async def request(self, **kwargs):
        return {"result": True}

    async def get_config(self, params):
        type(self).calls.append(("get_config", self._session_id, params["name"]))
        if type(self).failures:
            raise type(self).failures.pop(0)
        return {"table": [{"Enable": True}]}

    async def _call(self, method, *args):
        type(self).calls.append((method, self._session_id, args))
        if type(self).failures:
            raise type(self).failures.pop(0)
        if method == "status":
            return type("Status", (), {"speaker": True, "white_light": False})()
        if method == "caps":
            return {"SupportControlSpeaker": True}
        return {"result": True}

    async def get_coaxial_control_io_status(self, *args):
        return await self._call("status", *args)

    async def get_coaxial_control_io_caps(self, *args):
        return await self._call("caps", *args)

    async def set_coaxial_control_state(self, *args):
        return await self._call("control", *args)


@pytest.fixture
async def rpc2_client():
    FakeRpc2.logins = 0
    FakeRpc2.failures = []
    FakeRpc2.calls = []
    client_module._HOST_RPC2.clear()
    client = DahuaClient("u", "p", "camera", 80, 554, None, use_rpc2=True)
    with patch.object(client_module, "DahuaRpc2Client", FakeRpc2), patch.object(
        DahuaClient, "_new_rpc2_session", return_value=AsyncMock()
    ):
        yield client
        await client.close()
    client_module._HOST_RPC2.clear()


@pytest.mark.parametrize(
    "operation,expected",
    [
        ("status", {"status.Speaker": "On", "status.WhiteLight": "Off"}),
        ("caps", {"SupportControlSpeaker": True}),
        ("control", {"result": True}),
    ],
)
async def test_expired_session_relogin_and_retry(rpc2_client, operation, expected):
    FakeRpc2.failures = [EXPIRED]
    holder = await rpc2_client._shared_rpc2()
    old_keepalive = holder.keepalive
    if operation == "status":
        result = await rpc2_client.async_get_coaxial_control_io_status_rpc2()
    elif operation == "caps":
        result = await rpc2_client.async_get_coaxial_control_io_caps_rpc2()
    else:
        result = await rpc2_client.async_set_coaxial_control_state_rpc2(2, True)
    assert result == expected
    assert FakeRpc2.logins == 2
    assert [call[1] for call in FakeRpc2.calls] == ["session-1", "session-2"]
    assert old_keepalive.cancelled()
    assert holder.keepalive is not old_keepalive
    assert holder.keepalive is not None and not holder.keepalive.done()


async def test_expired_session_retries_only_once(rpc2_client):
    FakeRpc2.failures = [EXPIRED, EXPIRED]
    with pytest.raises(Rpc2MethodRefused):
        await rpc2_client.async_get_coaxial_control_io_status_rpc2()
    assert FakeRpc2.logins == 2
    assert len(FakeRpc2.calls) == 2


async def test_expired_message_without_code_retries(rpc2_client):
    FakeRpc2.failures = [
        Rpc2MethodRefused("expired", message="Session Is Out Of Date!")
    ]
    await rpc2_client.async_set_coaxial_control_state_rpc2(2, True)
    assert FakeRpc2.logins == 2
    assert len(FakeRpc2.calls) == 2


@pytest.mark.parametrize(
    "error",
    [
        Rpc2MethodRefused("unsupported", code=268959743, message="unknown method"),
        Rpc2MethodRefused("unsupported"),
    ],
)
async def test_other_refusal_does_not_relogin(rpc2_client, error):
    FakeRpc2.failures = [error]
    with pytest.raises(Rpc2MethodRefused):
        await rpc2_client.async_get_coaxial_control_io_caps_rpc2()
    assert FakeRpc2.logins == 1
    assert len(FakeRpc2.calls) == 1


async def test_refusal_exposes_response_fields():
    response = AsyncMock()
    response.text.return_value = (
        '{"result":false,"error":{"code":287637504,'
        '"message":"session is out of date!"}}'
    )
    session = AsyncMock()
    session.post.return_value = response
    rpc2 = DahuaRpc2Client("u", "p", "camera", 80, 554, session)
    with pytest.raises(Rpc2MethodRefused) as caught:
        await rpc2.request("CoaxialControlIO.getStatus")
    assert caught.value.code == 287637504
    assert caught.value.message == "session is out of date!"
    assert "code=287637504" in str(caught.value)


# --- the config-read path, which decides the transport -------------------------

async def test_an_expired_login_on_a_config_read_is_renewed(rpc2_client):
    """Without the retry the refusal reached _request, which read it as "does
    not serve this table" and stopped asking RPC2 for it for good."""
    FakeRpc2.failures = [EXPIRED]

    result = await rpc2_client._rpc2_get_config("MotionDetect")

    assert FakeRpc2.logins == 2
    assert [call[1] for call in FakeRpc2.calls] == ["session-1", "session-2"]
    assert result == {"table.MotionDetect[0].Enable": "true"}


async def test_a_config_read_retries_a_stale_login_only_once(rpc2_client):
    FakeRpc2.failures = [EXPIRED, EXPIRED]

    with pytest.raises(Rpc2MethodRefused):
        await rpc2_client._rpc2_get_config("MotionDetect")

    assert FakeRpc2.logins == 2


async def test_a_non_json_answer_is_not_a_permanent_verdict():
    """A device serving an HTML error page for one read is not a device that
    does not speak RPC2; two of them used to write the host off for good."""
    from custom_components.dahua.client import rpc2_failure_is_permanent

    class _Response:
        async def text(self):
            return "<html>503 Service Unavailable</html>"

    class _Session:
        async def post(self, url, json):
            return _Response()

    rpc2 = object.__new__(DahuaRpc2Client)
    rpc2._session = _Session()
    rpc2._session_id = "session"
    rpc2._id = 0
    rpc2._base = "http://camera"

    with pytest.raises(aiohttp.ClientConnectionError) as caught:
        await rpc2.request(method="configManager.getConfig", params={"name": "x"})

    assert rpc2_failure_is_permanent(caught.value) is False
