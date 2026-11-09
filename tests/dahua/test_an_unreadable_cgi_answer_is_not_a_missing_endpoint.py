"""#1176: a camera whose CGI answers cannot be read gets its identity over RPC2.

A DH-IPC-HDW2849TM-S-IL on firmware 3.120 answers every authenticated CGI call
with a complete 200 and then one stray header line. aiohttp rejects the whole
exchange and raises `ClientResponseError` with a status of 400 that the device
never sent. `ClientResponseError` subclasses `ClientError`, and every optional
read here is guarded on that, so the camera does not fail: it reports as
`Generic RTSP` on firmware `1.0`, takes a serial derived from its credentials,
and gets none of its config-backed entities.

@defre92 measured that the same camera answers RPC2 with the integration's own
credentials, which is what makes an answer possible at all.

Two halves, deliberately:

- the **discriminator**, driven through a real aiohttp over loopback, because
  the thing being matched is aiohttp's own wording and an upgrade that changes
  it has to redden a test rather than quietly put this camera back on a hash;
- the **routing**, driven through the real client methods, because what matters
  is that each identity read returns the device's answer instead of its
  fallback.
"""

import asyncio
import socket

import aiohttp
import pytest

from custom_components.dahua.client import (
    MANGLED_FRAMING_MESSAGE,
    DahuaClient,
    refusal_is_mangled_framing,
)

# The firmware's own answer shape, as far as it is known. The stray line is the
# one in @defre92's log; the 200 around it is inferred from curl reporting
# `Excess found in a read: excess = 33, size = 11`, which is exactly
# `X-Content-Type-Options: nosniff` plus CRLF after an 11 byte body.
STRAY_HEADER = b"X-Content-Type-Options: nosniff\r\n"

TRAILING_DATA = (
    b"HTTP/1.1 200 OK\r\n"
    b"Content-Type: text/plain\r\n"
    b"Content-Length: 11\r\n"
    b"Connection: close\r\n"
    b"\r\n"
    b"name=Camera" + STRAY_HEADER
)

# What a device sends when it means it. A recorder answers this to snapshot.cgi
# for a channel it does not have, which is why CONFIG_CGI_ABSENT lists only 404
# and 501 and why the status alone cannot be the test.
A_REAL_400 = (
    b"HTTP/1.1 400 Bad Request\r\n"
    b"Content-Length: 5\r\n"
    b"Connection: close\r\n"
    b"\r\n"
    b"Error"
)

A_REAL_404 = b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"


async def _raise_from(payload):
    """Serve `payload` raw on a loopback port and return what aiohttp raises.

    The callers take the suite's `socket_enabled` fixture, because
    pytest-homeassistant-custom-component blocks real sockets by default. A
    real one is the point here: the string being matched is aiohttp's, so
    serving the bytes is the only way to find out what aiohttp says about them.
    """
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    def serve():
        connection, _ = listener.accept()
        connection.recv(4096)
        connection.sendall(payload)
        connection.close()
        listener.close()

    served = asyncio.get_running_loop().run_in_executor(None, serve)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get("http://127.0.0.1:%d/x" % port) as response:
                response.raise_for_status()
                await response.read()
        return None
    except aiohttp.ClientResponseError as error:
        return error
    finally:
        await served


# ---------------------------------------------------------------------------
# The discriminator, against a real aiohttp
# ---------------------------------------------------------------------------


async def test_aiohttp_still_words_the_refusal_the_way_the_predicate_expects(
    socket_enabled,
):
    """The constant is aiohttp's wording, so an upgrade that changes it reddens here.

    Nothing promises `message` is stable across aiohttp releases. Matching on
    it is still the right call -- it is the only field that says what actually
    happened -- but it has to be pinned against the library rather than against
    another copy of the same string.
    """
    error = await _raise_from(TRAILING_DATA)

    assert error is not None, "aiohttp accepted the trailing bytes"
    assert error.status == 400, error.status
    assert error.message.startswith(MANGLED_FRAMING_MESSAGE), error.message
    # The 400 is synthesised, so there is no response to carry headers. Asserted
    # because the predicate's docstring offers it as the weaker second signal,
    # and a claim nothing checks is the kind that goes quietly false.
    assert error.headers is None


async def test_the_camera_shape_is_recognised(socket_enabled):
    assert refusal_is_mangled_framing(await _raise_from(TRAILING_DATA)) is True


async def test_a_real_400_from_the_device_is_not_recognised(socket_enabled):
    """The recorder's own 400 must keep falling back, not go asking RPC2.

    This is the whole reason the status is not the test. Widening
    CONFIG_CGI_ABSENT to 400 would have swept this up with it.
    """
    error = await _raise_from(A_REAL_400)

    assert error.status == 400, error.status
    assert error.message == "Bad Request", error.message
    assert refusal_is_mangled_framing(error) is False


async def test_a_404_is_not_recognised_as_mangled_framing(socket_enabled):
    """A missing endpoint is the other branch's business, not this one's."""
    assert refusal_is_mangled_framing(await _raise_from(A_REAL_404)) is False


@pytest.mark.parametrize(
    "status,message",
    [
        (400, "Bad Request"),
        (400, ""),
        (400, None),
        (401, MANGLED_FRAMING_MESSAGE),
        (404, MANGLED_FRAMING_MESSAGE),
        (500, MANGLED_FRAMING_MESSAGE),
    ],
)
def test_both_halves_of_the_predicate_are_required(status, message):
    """Neither the status nor the message alone is enough.

    A 401 is a refused login and must keep raising for reauth; a 404 belongs to
    the VTH branch. So the message is not consulted without the status either.
    """
    assert (
        refusal_is_mangled_framing(
            aiohttp.ClientResponseError(
                request_info=None, history=(), status=status, message=message
            )
        )
        is False
    )


def test_the_predicate_does_not_need_a_clientresponseerror():
    """It is handed whatever the caller caught, so it reads defensively."""
    assert refusal_is_mangled_framing(object()) is False
    assert refusal_is_mangled_framing(None) is False


# ---------------------------------------------------------------------------
# The routing, through the real client methods
# ---------------------------------------------------------------------------

# The camera's model and firmware are from #1176. **The class and the serial
# are stand-ins**: nobody has read them off this device, and the point of each
# test below is that whatever the camera says is what comes back, not that it
# says these. The serial is deliberately not a plausible Dahua one, so a reader
# cannot mistake it for a measurement.
CAMERA_ANSWERS = {
    "magicBox.getDeviceClass": {"result": True, "params": {"type": "IPC"}},
    "magicBox.getDeviceType": {
        "result": True,
        "params": {"type": "IPC-HDW2849TM-S-IL"},
    },
    "magicBox.getSoftwareVersion": {
        "result": True,
        "params": {"version": {"Version": "3.120.0000000.1.R"}},
    },
    "magicBox.getSerialNo": {"result": True, "params": {"sn": "NOT-A-REAL-SERIAL"}},
}


class _Rpc2:
    """Stands in for the shared RPC2 client and records what it was asked."""

    def __init__(self, answers):
        self.answers = answers
        self.asked = []

    async def request(self, method, params="unset", **kwargs):
        self.asked.append(method)
        answer = self.answers.get(method)
        if answer is None:
            raise AssertionError("%s is not answered in this test" % method)
        return answer


def _mangled(detail=True):
    """The exception aiohttp raises for the camera, built rather than served.

    The wording is pinned against a real aiohttp by the tests above, so these
    can be cheap. The C parser's suffix is included by default, because that is
    what runs unless AIOHTTP_NO_EXTENSIONS is set, and a predicate that only
    handled the short form would pass a whole file of these and still fail in
    the field.
    """
    message = MANGLED_FRAMING_MESSAGE
    if detail:
        message += ":\n\n  b'name=Camera%s'\n                ^" % (
            STRAY_HEADER.decode().strip(),
        )
    return aiohttp.ClientResponseError(None, None, status=400, message=message)


def _client(error, answers=None):
    """A client whose every magicBox.cgi read raises `error`, with RPC2 behind it."""
    client = DahuaClient("u", "p", "10.0.0.17", 80, 554, None)
    rpc2 = _Rpc2(CAMERA_ANSWERS if answers is None else answers)

    async def get(url, verify_ok=False):
        raise error

    async def shared_call(action):
        return await action(rpc2)

    client.get = get
    client._rpc2_shared_call = shared_call
    return client, rpc2


async def test_the_model_comes_from_the_device_rather_than_generic_rtsp():
    client, _ = _client(_mangled())

    assert (await client.get_device_type())["type"] == "IPC-HDW2849TM-S-IL"


async def test_the_firmware_comes_from_the_device_rather_than_1_0():
    client, _ = _client(_mangled())

    assert (await client.get_software_version())["version"] == "3.120.0000000.1.R"


async def test_the_class_comes_from_the_device():
    """And in the shape the CGI answer would have had: stripped and upper-cased."""
    client, _ = _client(_mangled())

    assert await client.async_get_device_class() == "IPC"


async def test_the_name_is_the_model_rather_than_a_hash_of_the_credentials():
    client, _ = _client(_mangled())

    answer = await client.get_machine_name()

    assert answer["name"] == "IPC-HDW2849TM-S-IL"
    assert answer["name"] != client.credential_hash()
    assert client.identity_derived_from_credentials is False


async def test_the_serial_is_the_reported_one_and_the_entry_is_held_to_it():
    """The serial is the one that matters: the hash moves when the password does (#805).

    `identity_from_rpc2` is what `async_setup_entry` reads to migrate the
    registry onto the reported serial and then refuse to go back to the hash
    (#1141), so adopting the serial without setting it would be the half fix.
    """
    client, _ = _client(_mangled())

    assert client.identity_from_rpc2 is False

    answer = await client.async_get_system_info()

    assert answer == {"serialNumber": "NOT-A-REAL-SERIAL"}
    assert client.identity_from_rpc2 is True
    assert client.identity_derived_from_credentials is False


async def test_the_short_form_of_the_message_is_handled_too():
    """AIOHTTP_NO_EXTENSIONS=1 gives the message with no offending bytes appended."""
    client, _ = _client(_mangled(detail=False))

    assert (await client.get_device_type())["type"] == "IPC-HDW2849TM-S-IL"


async def test_the_device_is_asked_once_and_the_answer_remembered():
    """Five identity reads, one set of RPC2 calls.

    These devices log every login and #577 and #603 are what happens without
    that discipline, so the memo is not a micro-optimisation.
    """
    client, rpc2 = _client(_mangled())

    await client.async_get_system_info()
    await client.get_device_type()
    await client.get_software_version()
    await client.get_machine_name()
    await client.async_get_device_class()

    assert rpc2.asked == [
        "magicBox.getDeviceClass",
        "magicBox.getDeviceType",
        "magicBox.getSoftwareVersion",
        "magicBox.getSerialNo",
    ]


async def test_a_real_400_from_the_device_keeps_every_fallback():
    """The recorder's own 400 must not send anything to RPC2.

    This is the guard that makes the narrow predicate worth having. Widening
    CONFIG_CGI_ABSENT to 400 would have given every device that answers a real
    400 an identity read it never used to pay for.
    """
    real = aiohttp.ClientResponseError(None, None, status=400, message="Bad Request")
    client, rpc2 = _client(real)

    assert (await client.get_device_type())["type"] == "Generic RTSP"
    assert (await client.get_software_version())["version"] == "1.0"
    assert (await client.async_get_system_info()) == {
        "serialNumber": client.credential_hash()
    }
    assert client.identity_from_rpc2 is False
    assert rpc2.asked == []


async def test_a_404_on_a_camera_still_keeps_the_fallback():
    """The VTH gate is untouched: only a monitor answers for an absent CGI.

    A camera that has been running as `Generic RTSP` for years must not change
    shape because it happens to answer RPC2 (test_identity_fallback_is_reported).
    The new route is for devices whose CGI answers are unreadable, which is a
    different refusal and a device with no working shape to preserve.
    """
    absent = aiohttp.ClientResponseError(None, None, status=404)
    client, rpc2 = _client(absent)

    assert (await client.get_device_type())["type"] == "Generic RTSP"
    assert (await client.get_software_version())["version"] == "1.0"
    assert client.identity_from_rpc2 is False
    # Asked, and refused for not being a VTH. The short-circuit is why only one
    # call was spent finding that out.
    assert rpc2.asked == ["magicBox.getDeviceClass"]


async def test_an_unrelated_refusal_still_raises_from_the_class_read():
    """async_get_device_class raises for anything it cannot answer, as before."""
    client, rpc2 = _client(
        aiohttp.ClientResponseError(None, None, status=500, message="Internal")
    )

    with pytest.raises(aiohttp.ClientResponseError):
        await client.async_get_device_class()
    assert rpc2.asked == []


async def test_a_camera_that_names_itself_but_not_its_serial_still_sets_up():
    """The case the gate on the re-raise exists for, and the only one that shows it.

    RPC2 names the model and the firmware but gives no serial. For a monitor
    that combination deliberately re-raises: it has already been given a real
    identity, and falling back to the hash would duplicate every entity (#1141).
    For this camera it must not, because it has never had one -- raising would
    turn a badly identified entry into no entry at all.

    Written after a mutation run: removing `e.status in self.CONFIG_CGI_ABSENT`
    from that condition reddened nothing, because the other test here has RPC2
    answer so little that `identity` is None and the branch is never reached.
    An unproven guard is an untested one.
    """
    client, _ = _client(
        _mangled(),
        answers={
            **CAMERA_ANSWERS,
            "magicBox.getSerialNo": {"result": True, "params": {"sn": ""}},
        },
    )

    # The hash, not a raise.
    assert (await client.async_get_system_info()) == {
        "serialNumber": client.credential_hash()
    }
    assert client.identity_from_rpc2 is False
    # And the model it did name is still used, so the entry is better off than
    # it was even without a serial.
    assert (await client.get_device_type())["type"] == "IPC-HDW2849TM-S-IL"


async def test_a_transient_rpc2_failure_is_retried_on_the_next_refresh():
    """A memoised None plus a raise would be a permanent failure, not a retry.

    `async_get_system_info` is called from the one-time init block in
    `_async_update_data`, which leaves `initialized` False on a raise and
    re-enters **on the same client** next refresh. Both identity readers
    remember a None, so a single RPC2 timeout on an entry that is held to a
    reported serial would otherwise be remembered for the life of the entry and
    the device would never set up again.

    The VTH half of this reset already existed. The new memo was missed when it
    was added, which none of this change's mutations caught, so it is asserted
    here with a client that fails once and then answers.
    """
    client, rpc2 = _client(_mangled())
    # The policy an entry acquires once it has adopted a reported serial (#1141).
    client._require_reported_serial = True

    calls = {"n": 0}
    real_request = rpc2.request

    async def fails_once(method, params="unset", **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise TimeoutError("the device did not answer in time")
        return await real_request(method, params, **kwargs)

    rpc2.request = fails_once

    # First refresh: nothing readable over either transport, so it raises
    # rather than silently going back to the credential hash.
    with pytest.raises(aiohttp.ClientResponseError):
        await client.async_get_system_info()

    # Next refresh, same client. The memo must not have survived.
    assert (await client.async_get_system_info()) == {
        "serialNumber": "NOT-A-REAL-SERIAL"
    }
    assert client.identity_from_rpc2 is True


async def test_a_device_that_will_not_name_itself_over_rpc2_falls_back_as_before():
    """RPC2 answering nothing useful must not stop the camera setting up at all.

    The escape that re-raises for a monitor is deliberately still gated on an
    absent CGI, because a camera here has never had a real identity: raising
    would leave it with no entry rather than a badly named one.
    """
    client, _ = _client(
        _mangled(),
        answers={
            "magicBox.getDeviceClass": {"result": True, "params": {"type": "IPC"}},
            "magicBox.getDeviceType": {"result": True, "params": {"type": ""}},
            "magicBox.getSoftwareVersion": {"result": True, "params": {}},
            "magicBox.getSerialNo": {"result": True, "params": {"sn": ""}},
        },
    )

    assert (await client.async_get_system_info()) == {
        "serialNumber": client.credential_hash()
    }
    assert client.identity_from_rpc2 is False
    assert (await client.get_device_type())["type"] == "Generic RTSP"
