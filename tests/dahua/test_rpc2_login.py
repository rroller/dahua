"""RPC2 accepts device JSON framing and preserves session ID types."""

import json
from unittest.mock import AsyncMock

import aiohttp
import pytest

from custom_components.dahua.rpc2 import DahuaRpc2Client


class _Response:
    def __init__(self, payload, prefix=""):
        self.payload = payload
        self.prefix = prefix

    async def text(self):
        return self.prefix + json.dumps(self.payload)


class _Session:
    def __init__(self, authenticated_response, prefix=""):
        self.prefix = prefix
        self.responses = iter(
            [
                {
                    "result": False,
                    "session": 777,
                    "params": {"realm": "test-realm", "random": "test-challenge"},
                },
                authenticated_response,
                {"result": True},
            ]
        )
        self.calls = []

    async def post(self, url, json):
        self.calls.append((url, json))
        return _Response(next(self.responses), self.prefix)


@pytest.mark.parametrize("session_id", [12345, "session-12345"])
@pytest.mark.parametrize("prefix", ["", "\ufeff"])
async def test_login_accepts_numeric_and_string_sessions_without_coercion(
    session_id, prefix
):
    response = {"result": True, "session": session_id}
    session = _Session(response, prefix)
    client = DahuaRpc2Client("user", "password", "camera.invalid", 80, 554, session)

    assert await client.login() == response
    await client.request("global.keepAlive")

    assert [url for url, _ in session.calls] == [
        "http://camera.invalid:80/RPC2_Login",
        "http://camera.invalid:80/RPC2_Login",
        "http://camera.invalid:80/RPC2",
    ]
    assert session.calls[1][1]["session"] == 777
    sent = session.calls[2][1]
    assert sent["method"] == "global.keepAlive"
    assert sent["session"] == session_id
    assert type(sent["session"]) is type(session_id)


@pytest.mark.parametrize("session_id", [None, "", 0, True, False, 1.5, [], {}])
async def test_login_rejects_invalid_sessions(session_id):
    session = _Session({"result": True, "session": session_id})
    client = DahuaRpc2Client("user", "password", "camera.invalid", 80, 554, session)

    with pytest.raises(ConnectionError, match="missing session"):
        await client.login()

    assert len(session.calls) == 2


async def test_login_rejects_a_response_without_a_session():
    session = _Session({"result": True})
    client = DahuaRpc2Client("user", "password", "camera.invalid", 80, 554, session)

    with pytest.raises(ConnectionError, match="missing session"):
        await client.login()

    assert len(session.calls) == 2


@pytest.mark.parametrize("body", ["not JSON", "\ufeffnot JSON", "\ufeff{"])
async def test_request_still_rejects_malformed_json(body):
    session = AsyncMock()
    session.post.return_value.text.return_value = body
    client = DahuaRpc2Client("user", "password", "camera.invalid", 80, 554, session)

    with pytest.raises(aiohttp.ClientConnectionError, match="not JSON"):
        await client.request("global.keepAlive")
