"""One press of Reboot sends one reboot (#1176, the review of #1246).

#1246 sent a reboot whose CGI answer could not be read again over RPC2. The
review simulated a device that takes the CGI request, answers it unreadably and
is still up for a moment: one press scheduled two reboots. An unreadable answer
is a complete, correct 200 followed by bytes no parser accepts, so it says
nothing about whether the request was acted on.

A reboot has nothing to read back, so the transport is chosen before anything
is sent, from what an earlier READ showed about this device. These tests serve
the bytes over loopback and drive the real request path, because what has to be
proved is that the path which meets the unreadable answer is the one that
records it -- and that a reboot meeting it records nothing.
"""

import json
import pathlib

import aiohttp
import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.dahua import client as client_module
from custom_components.dahua.client import DahuaClient

from .test_a_config_read_that_cannot_be_read import (
    A_CLEAN_200,
    A_REAL_400,
    A_REAL_404,
    MACHINE_NAME,
    MOTION_DETECT,
    UNREADABLE,
    _Rpc2Reads,
    _serving,
)
from .test_event_stream import _polled, _UnreadableSession

REBOOT_OVER_CGI = b"GET /cgi-bin/magicBox.cgi?action=reboot "


class _Rpc2:
    """Stands in for the shared RPC2 session and records what it was asked."""

    def __init__(self):
        self.asked = []

    async def request(self, method, params="unset", **kwargs):
        self.asked.append(method)
        return {"result": True, "params": None}


def _client(server, session):
    client = DahuaClient("u", "p", "127.0.0.1", server.port, 554, session)
    rpc2 = _Rpc2()

    async def shared_call(action):
        return await action(rpc2)

    client._rpc2_shared_call = shared_call
    client._rpc2_get_config = _Rpc2Reads()
    return client, rpc2


def _reboots(server, rpc2):
    """Every reboot the device was sent, whichever way it came."""
    over_cgi = [r for r in server.requests if r.startswith(REBOOT_OVER_CGI)]
    over_rpc2 = [m for m in rpc2.asked if m == "magicBox.reboot"]
    return len(over_cgi), len(over_rpc2)


async def test_a_device_that_took_the_reboot_and_answered_unreadably_gets_one(
    socket_enabled,
):
    """The review's device, and what it was sent before: (1, 1)."""
    async with _serving(UNREADABLE, UNREADABLE) as server:
        async with aiohttp.ClientSession() as session:
            client, rpc2 = _client(server, session)

            with pytest.raises(HomeAssistantError) as raised:
                await client.reboot()

    assert _reboots(server, rpc2) == (1, 0)
    assert raised.value.translation_key == "reboot_outcome_unknown"
    assert client._device not in client_module._HOST_CGI_UNREADABLE


@pytest.mark.parametrize("read", [MOTION_DETECT, MACHINE_NAME])
async def test_after_a_read_came_back_unreadable_the_reboot_goes_over_rpc2_only(
    socket_enabled, read
):
    """#1176's camera: every read it has ever answered says which this is."""
    async with _serving(UNREADABLE, UNREADABLE) as server:
        async with aiohttp.ClientSession() as session:
            client, rpc2 = _client(server, session)
            try:
                await client._request(read)
            except aiohttp.ClientResponseError:
                # A read with no RPC2 route still raises. It has been seen to
                # be unreadable all the same, which is all this needs.
                pass
            assert len(server.requests) == 1

            assert await client.reboot() == {"result": True}

    assert _reboots(server, rpc2) == (0, 1)
    assert len(server.requests) == 1, "the reboot was also tried over CGI"


async def test_an_unreadable_subscription_says_so_too(monkeypatch):
    """The third place a read meets it, and the only one a camera whose config
    already goes over RPC2 by option still makes over CGI on every start."""
    _polled(monkeypatch)
    client = DahuaClient("u", "p", "d", 80, 554, _UnreadableSession())
    rpc2 = _Rpc2()

    async def shared_call(action):
        return await action(rpc2)

    async def get(url, verify_ok=False):
        raise AssertionError("the reboot went over CGI: %s" % url)

    client._rpc2_shared_call = shared_call
    client.get = get

    await client.stream_events(lambda data, channel: None, ["VideoMotion"], 0)

    assert await client.reboot() == {"result": True}
    assert rpc2.asked == ["magicBox.reboot"]


@pytest.mark.parametrize(
    "answer, expected",
    [
        # A real refusal of a read says the request was wrong, not the framing.
        (A_REAL_400, (1, 0)),
        # No config endpoint is not an unreadable one. This device's reboot is
        # still tried over CGI first, exactly as before.
        (A_REAL_404, (1, 0)),
        (A_CLEAN_200, (1, 0)),
    ],
)
async def test_any_other_answer_to_a_read_leaves_the_reboot_on_cgi(
    socket_enabled, answer, expected
):
    reboot_ok = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nOK"
    async with _serving(answer, reboot_ok) as server:
        async with aiohttp.ClientSession() as session:
            client, rpc2 = _client(server, session)
            try:
                await client._request(MOTION_DETECT)
            except aiohttp.ClientResponseError:
                pass
            assert client._device not in client_module._HOST_CGI_UNREADABLE

            await client.reboot()

    assert _reboots(server, rpc2) == expected


async def test_an_unreadable_write_does_not_say_so(socket_enabled):
    """A write that cannot be read may have happened, which is the very thing
    this memo must not be built on."""
    write = "/cgi-bin/configManager.cgi?action=setConfig&MotionDetect[0].Enable=true"
    async with _serving(UNREADABLE) as server:
        async with aiohttp.ClientSession() as session:
            client, _rpc2 = _client(server, session)
            try:
                await client._request(write, allow_rpc2=False)
            except aiohttp.ClientResponseError:
                pass

    assert len(server.requests) == 1
    assert client._device not in client_module._HOST_CGI_UNREADABLE


def test_the_message_exists_and_names_the_device():
    """Raised with a translation key, so a missing string would show the user
    the key instead of the sentence."""
    strings = json.loads(
        (
            pathlib.Path(client_module.__file__).parent / "translations" / "en.json"
        ).read_text(encoding="utf-8")
    )
    message = strings["exceptions"]["reboot_outcome_unknown"]["message"]

    assert "{device}" in message
    assert "not known" in message
