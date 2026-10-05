"""One-shot DHIP, and what the add form says about a device that serves no HTTP.

#949 measured a VTH5221D (3.000.0012000.0.R) with only 5000 and 37777 open: no HTTP at
all, a DHIP login that completes, and magicBox.getDeviceType and configManager.getConfig
answering over DHIP. The add form already noticed the Dahua ports answering and said
`http_service_off`: "turn on HTTP and CGI", a switch such a monitor does not have.

The client is exercised here against a fake device on a real socket, speaking the wire
format vto.py does (its convert_message header, its login challenge and its password
hash), so what is pinned is the protocol and not a double of the client. It is not a
measurement: the first run against the VTH5221D is what this file cannot provide.
"""

import asyncio
import json
import struct

import pytest

from custom_components.dahua import config_flow, dhip
from custom_components.dahua.dhip import DhipLoginRefused, async_dhip_session
from custom_components.dahua.vto import DahuaVTOClient

REALM = "Login to 8d0f9f9a"
RANDOM = "1234567890"


def _device_frame(message):
    """A reply as a device sends one: vto.py's 32-byte header, then the JSON on one line
    and a newline. Not convert_message, which pretty-prints: newlines inside the JSON
    would split one reply in two, and vto.py, which splits on newlines, has been
    reading real doorbells' replies, so they are compact."""
    body = json.dumps(message, separators=(",", ":")).encode("utf-8")
    header = struct.pack(">L", 0x20000000) + struct.pack(">L", 0x44484950)
    header += struct.pack(">d", 0)
    header += struct.pack("<L", len(body)) + struct.pack("<L", 0)
    header += struct.pack("<L", len(body)) + struct.pack("<L", 0)
    return header + body + b"\n"


class _FakeDevice:
    """A DHIP device: a login challenge, a password check, and a few answers."""

    def __init__(
        self,
        username="admin",
        password="right",
        answers=None,
        refuse_before_challenge=None,
        silent=False,
        close_after_connect=False,
        notification_first=False,
        one_byte_at_a_time=False,
        no_challenge=False,
        hang_up_after_request=False,
        challenge_message=None,
        session=777,
    ):
        self.username = username
        self.password = password
        self.answers = (
            {"magicBox.getDeviceClass": {"type": "VTH"}} if answers is None else answers
        )
        self.refuse_before_challenge = refuse_before_challenge
        self.silent = silent
        self.close_after_connect = close_after_connect
        self.notification_first = notification_first
        self.one_byte_at_a_time = one_byte_at_a_time
        self.no_challenge = no_challenge
        self.hang_up_after_request = hang_up_after_request
        self.challenge_message = challenge_message
        self.session = session
        self.received = []
        self.logins = 0
        self.server = None
        self.port = None

    async def start(self):
        self.server = await asyncio.start_server(self._client, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def stop(self):
        self.server.close()
        await self.server.wait_closed()

    async def _send(self, writer, message):
        frame = _device_frame(message)
        if self.one_byte_at_a_time:
            for i in range(len(frame)):
                writer.write(frame[i : i + 1])
                await writer.drain()
        else:
            writer.write(frame)
            await writer.drain()

    async def _client(self, reader, writer):
        if self.close_after_connect:
            writer.close()
            return
        try:
            while True:
                header = await reader.readexactly(32)
                assert header[4:8] == b"DHIP", header
                length = struct.unpack("<L", header[16:20])[0]
                request = json.loads(await reader.readexactly(length))
                self.received.append(request)
                if self.hang_up_after_request:
                    break
                if self.silent:
                    continue
                if self.notification_first:
                    await self._send(
                        writer,
                        {
                            "id": 999,
                            "method": "client.notifyEventStream",
                            "params": {"eventList": []},
                        },
                    )
                await self._answer(writer, request)
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()

    async def _answer(self, writer, request):
        method, params, rid = (
            request["method"],
            request.get("params") or {},
            request["id"],
        )
        if method == "global.login" and params.get("password") == "":
            self.logins += 1
            if self.refuse_before_challenge:
                await self._send(
                    writer,
                    {
                        "id": rid,
                        "result": False,
                        "session": 0,
                        "error": self.refuse_before_challenge,
                    },
                )
                return
            if self.no_challenge:
                await self._send(writer, {"id": rid, "result": True, "params": {}})
                return
            await self._send(
                writer,
                {
                    "id": rid,
                    "result": False,
                    "session": self.session,
                    "error": {
                        "code": 268632079,
                        "message": self.challenge_message
                        or "Component error: login challenge!",
                    },
                    "params": {
                        "encryption": "Default",
                        "random": RANDOM,
                        "realm": REALM,
                    },
                },
            )
        elif method == "global.login":
            expected = DahuaVTOClient._get_hashed_password(
                RANDOM, REALM, self.username, self.password
            )
            if (
                params.get("userName") == self.username
                and params.get("password") == expected
                and request.get("session") == self.session
            ):
                await self._send(
                    writer,
                    {
                        "id": rid,
                        "result": True,
                        "session": self.session,
                        "params": {"keepAliveInterval": 60},
                    },
                )
            else:
                await self._send(
                    writer,
                    {
                        "id": rid,
                        "result": False,
                        "session": self.session,
                        "error": {"code": 268632085, "message": "Invalid password!"},
                    },
                )
        elif method == "global.logout":
            await self._send(writer, {"id": rid, "result": True})
        elif method in self.answers:
            await self._send(
                writer,
                {
                    "id": rid,
                    "result": True,
                    "params": self.answers[method],
                    "session": request.get("session"),
                },
            )
        else:
            await self._send(
                writer,
                {
                    "id": rid,
                    "result": False,
                    "error": {"code": 268894210, "message": "unknown"},
                },
            )


@pytest.fixture
async def device(socket_enabled):
    """Fake devices on 127.0.0.1. socket_enabled is the suite's own switch for a test
    that needs a real local socket, as test_the_tcp_probe.py uses."""
    made = []

    async def make(**kwargs):
        d = await _FakeDevice(**kwargs).start()
        made.append(d)
        return d

    yield make
    for d in made:
        await d.stop()


@pytest.fixture(autouse=True)
def _short_replies(monkeypatch):
    """A silent device is waited on for this long; the real five seconds would hit the
    suite's nine second limit."""
    monkeypatch.setattr(dhip, "DHIP_REPLY_TIMEOUT_SECONDS", 0.3)


def _methods(d):
    return [request["method"] for request in d.received]


# --- logging in ------------------------------------------------------------------


async def test_the_right_password_logs_in_and_answers(device):
    d = await device()

    async with async_dhip_session("127.0.0.1", "admin", "right", port=d.port) as s:
        reply = await s.call("magicBox.getDeviceClass")

    assert reply["params"] == {"type": "VTH"}
    assert _methods(d) == [
        "global.login",
        "global.login",
        "magicBox.getDeviceClass",
        "global.logout",
    ]


async def test_the_login_is_the_challenge_then_the_hash(device):
    """The second login carries the session from the challenge and vto.py's hash, which
    the fake checks, so a wrong realm, random or session is a refusal here too."""
    d = await device()

    async with async_dhip_session("127.0.0.1", "admin", "right", port=d.port):
        pass

    first, second = d.received[0], d.received[1]
    assert first["params"]["password"] == "" and first["session"] == 0
    assert second["session"] == 777
    assert second["params"]["password"] == DahuaVTOClient._get_hashed_password(
        RANDOM, REALM, "admin", "right"
    )
    assert second["params"]["authorityType"] == "Default"


async def test_requests_after_the_login_carry_its_session(device):
    d = await device()

    async with async_dhip_session("127.0.0.1", "admin", "right", port=d.port) as s:
        await s.call("magicBox.getDeviceClass")

    assert d.received[2]["session"] == 777


async def test_a_session_given_as_a_string_is_carried_as_given(device):
    """The VTH5221D's session came back quoted in #949 (redacted, so not certain). Either
    way it is echoed exactly, never converted."""
    d = await device(session="a1b2c3d4")

    async with async_dhip_session("127.0.0.1", "admin", "right", port=d.port) as s:
        await s.call("magicBox.getDeviceClass")

    assert [r["session"] for r in d.received[1:3]] == ["a1b2c3d4", "a1b2c3d4"]


async def test_a_wrong_password_is_a_refusal(device):
    d = await device()

    with pytest.raises(DhipLoginRefused) as err:
        async with async_dhip_session("127.0.0.1", "admin", "wrong", port=d.port):
            pass

    assert err.value.code == 268632085


async def test_a_refused_login_is_tried_once_and_not_logged_out_of(device):
    """A Dahua device locks an account after a few refused logins. And a refusal grants
    no session, so there is nothing to log out of."""
    d = await device()

    with pytest.raises(DhipLoginRefused):
        async with async_dhip_session("127.0.0.1", "admin", "wrong", port=d.port):
            pass

    assert d.logins == 1
    assert "global.logout" not in _methods(d)


async def test_a_refusal_before_the_challenge_is_still_a_refusal(device):
    """A locked account answers the first login with an error and no challenge. The
    device answered, so it is not a connection fault."""
    d = await device(
        refuse_before_challenge={"code": 268632081, "message": "User locked!"}
    )

    with pytest.raises(DhipLoginRefused) as err:
        async with async_dhip_session("127.0.0.1", "admin", "right", port=d.port):
            pass

    assert err.value.code == 268632081
    assert "global.logout" not in _methods(d)


async def test_an_answer_that_is_neither_a_challenge_nor_a_refusal_is_not_a_login(
    device,
):
    d = await device(no_challenge=True)

    with pytest.raises(ValueError):
        async with async_dhip_session("127.0.0.1", "admin", "right", port=d.port):
            pass


async def test_a_device_that_never_answers_times_out(device):
    d = await device(silent=True)

    with pytest.raises(asyncio.TimeoutError):
        async with async_dhip_session("127.0.0.1", "admin", "right", port=d.port):
            pass


async def test_a_device_that_hangs_up_is_a_connection_error(device):
    d = await device(close_after_connect=True)

    with pytest.raises((ConnectionError, asyncio.IncompleteReadError)):
        async with async_dhip_session("127.0.0.1", "admin", "right", port=d.port):
            pass


async def test_a_device_that_hangs_up_before_answering_is_a_connection_error(device):
    """It read the request and closed. The reply is not coming, and waiting for it
    must end at the closed socket rather than spin on it."""
    d = await device(hang_up_after_request=True)

    with pytest.raises(ConnectionError):
        async with async_dhip_session("127.0.0.1", "admin", "right", port=d.port):
            pass


async def test_a_first_reply_that_is_not_the_challenge_is_refused(device):
    """It carries a random and an error, but not the challenge: a device saying no in
    a shape that happens to include those fields. Hashing a password against it would
    be a second login attempt the device never invited."""
    d = await device(challenge_message="User not valid!")

    with pytest.raises(DhipLoginRefused):
        async with async_dhip_session("127.0.0.1", "admin", "right", port=d.port):
            pass

    assert d.logins == 1 and len(d.received) == 1


async def test_nothing_listening_is_a_connection_error(socket_enabled):
    server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    server.close()
    await server.wait_closed()

    with pytest.raises(OSError):
        async with async_dhip_session("127.0.0.1", "admin", "right", port=port):
            pass


async def test_a_notification_before_the_reply_is_not_taken_for_it(device):
    d = await device(notification_first=True)

    async with async_dhip_session("127.0.0.1", "admin", "right", port=d.port) as s:
        reply = await s.call("magicBox.getDeviceClass")

    assert reply["params"] == {"type": "VTH"}


async def test_a_reply_split_across_reads_is_put_back_together(device):
    d = await device(one_byte_at_a_time=True)

    async with async_dhip_session("127.0.0.1", "admin", "right", port=d.port) as s:
        reply = await s.call("magicBox.getDeviceClass")

    assert reply["params"] == {"type": "VTH"}


# --- the add form ----------------------------------------------------------------


def _at(d):
    """config_flow's call, pointed at the fake device's port."""

    def session(address, username, password, **kwargs):
        return async_dhip_session(address, username, password, port=d.port)

    return session


async def test_a_vth_with_no_http_is_named_instead_of_told_to_switch_http_on(
    device, monkeypatch
):
    d = await device()
    monkeypatch.setattr(config_flow, "async_dhip_session", _at(d))

    assert (
        await config_flow.async_explain_http_service_off(
            "127.0.0.1", "admin", "right", "http_service_off"
        )
        == "vth_without_http"
    )


async def test_wrong_credentials_are_said_to_be_wrong(device, monkeypatch):
    """Before, the form said "switch HTTP on" whatever the password was."""
    d = await device()
    monkeypatch.setattr(config_flow, "async_dhip_session", _at(d))

    assert (
        await config_flow.async_explain_http_service_off(
            "127.0.0.1", "admin", "wrong", "http_service_off"
        )
        == "auth"
    )


@pytest.mark.parametrize("device_class", ["IPC", "VTO", "NVR", "VTHX", ""])
async def test_any_other_device_keeps_the_switch_http_on_message(
    device, monkeypatch, device_class
):
    """A camera with its web service off is exactly what that message is for."""
    d = await device(answers={"magicBox.getDeviceClass": {"type": device_class}})
    monkeypatch.setattr(config_flow, "async_dhip_session", _at(d))

    assert (
        await config_flow.async_explain_http_service_off(
            "127.0.0.1", "admin", "right", "http_service_off"
        )
        == "http_service_off"
    )


async def test_a_vth_that_only_says_its_model_is_still_named(device, monkeypatch):
    """Measured in #949 on a VTH5221D (3.000.0012000.0.R): magicBox.getDeviceType answers
    "VTH5221D" over DHIP. getDeviceClass has not been measured there."""
    d = await device(answers={"magicBox.getDeviceType": {"type": "VTH5221D"}})
    monkeypatch.setattr(config_flow, "async_dhip_session", _at(d))

    assert (
        await config_flow.async_explain_http_service_off(
            "127.0.0.1", "admin", "right", "http_service_off"
        )
        == "vth_without_http"
    )


async def test_the_model_is_not_asked_when_the_class_answered(device, monkeypatch):
    """A device that says it is a camera is a camera, whatever its model string."""
    d = await device(
        answers={
            "magicBox.getDeviceClass": {"type": "IPC"},
            "magicBox.getDeviceType": {"type": "VTH5221D"},
        }
    )
    monkeypatch.setattr(config_flow, "async_dhip_session", _at(d))

    assert (
        await config_flow.async_explain_http_service_off(
            "127.0.0.1", "admin", "right", "http_service_off"
        )
        == "http_service_off"
    )
    assert "magicBox.getDeviceType" not in _methods(d)


@pytest.mark.parametrize("model", ["IPC-HFW2431S", "VTO2211G", "XVTH12", ""])
async def test_a_model_that_is_not_a_vth_keeps_the_message(device, monkeypatch, model):
    d = await device(answers={"magicBox.getDeviceType": {"type": model}})
    monkeypatch.setattr(config_flow, "async_dhip_session", _at(d))

    assert (
        await config_flow.async_explain_http_service_off(
            "127.0.0.1", "admin", "right", "http_service_off"
        )
        == "http_service_off"
    )


async def test_a_device_that_will_not_say_its_class_keeps_the_message(
    device, monkeypatch
):
    d = await device(answers={})
    monkeypatch.setattr(config_flow, "async_dhip_session", _at(d))

    assert (
        await config_flow.async_explain_http_service_off(
            "127.0.0.1", "admin", "right", "http_service_off"
        )
        == "http_service_off"
    )


async def test_no_dhip_login_at_all_keeps_the_message(device, monkeypatch):
    d = await device(silent=True)
    monkeypatch.setattr(config_flow, "async_dhip_session", _at(d))

    assert (
        await config_flow.async_explain_http_service_off(
            "127.0.0.1", "admin", "right", "http_service_off"
        )
        == "http_service_off"
    )


@pytest.mark.parametrize(
    "reason",
    [
        "cannot_connect",
        "https_available",
        "auth",
        "timeout",
        "ssl_error",
        "cgi_disabled",
        "unexpected_reply",
    ],
)
async def test_only_that_one_message_ever_leads_to_a_dhip_login(
    device, monkeypatch, reason
):
    """Every other path either reached HTTP or found nothing listening. A 401 in
    particular has already spent one login, and a second over DHIP would be one more
    towards the lockout."""
    d = await device()
    monkeypatch.setattr(config_flow, "async_dhip_session", _at(d))

    assert (
        await config_flow.async_explain_http_service_off(
            "127.0.0.1", "admin", "right", reason
        )
        == reason
    )
    assert d.received == []


def test_the_new_message_has_a_translation():
    import pathlib

    path = (
        pathlib.Path(__file__).parents[2]
        / "custom_components"
        / "dahua"
        / "translations"
        / "en.json"
    )
    strings = json.loads(path.read_text(encoding="utf-8"))["config"]["error"]
    assert "vth_without_http" in strings


async def test_the_credential_test_asks_after_refining(monkeypatch):
    """Wired in after the port probe, which is what turns "cannot connect" into
    `http_service_off` in the first place."""
    from aiohttp import ClientConnectorError
    from custom_components.dahua.client import DahuaClient

    async def refused(self):
        raise ConnectionRefusedError(111, "Connect call failed")

    async def refine(address, reason):
        return "http_service_off"

    asked = []

    async def explain(address, username, password, reason):
        asked.append((address, username, password, reason))
        return "vth_without_http"

    monkeypatch.setattr(DahuaClient, "get_machine_name", refused)
    monkeypatch.setattr(config_flow, "async_refine_connection_failure", refine)
    monkeypatch.setattr(config_flow, "async_explain_http_service_off", explain)

    flow = object.__new__(config_flow.DahuaFlowHandler)
    data, error = await flow._test_credentials(
        "admin", "right", "10.0.0.30", 80, 554, 0
    )

    assert (data, error) == (None, "vth_without_http")
    assert asked == [("10.0.0.30", "admin", "right", "http_service_off")]
