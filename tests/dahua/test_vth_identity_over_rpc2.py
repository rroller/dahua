"""An indoor monitor (VTH) is identified over RPC2 instead of as `Generic RTSP`.

A VTH serves no CGI: measured on a VTH2421F-P on 4.800.0000000.1.R, magicBox.cgi
answers 404 to getDeviceType, getSoftwareVersion and getDeviceClass, so the client
fell back on every question and the device page said `Generic RTSP`, firmware `1.0`.
Over RPC2 the same device answers all three:

    magicBox.getDeviceClass      {"type": "VTH"}
    magicBox.getDeviceType       {"type": "VTH2421F-P"}
    magicBox.getSoftwareVersion  {"version": {"Version": "4.800.0000000.1.R",
                                              "BuildDate": "2025-01-23", ...}}

The other half matters as much. Which entities a device gets is decided from its
model, so every *other* device whose CGI is absent must keep exactly the fallback
identity it has had (test_identity_fallback_is_reported.py). Only a device that says
`VTH` over RPC2 changes, and a VTH has never been supported, so nobody depends on how
it looked.
"""

import aiohttp
import pytest

from custom_components.dahua.client import DahuaClient, rpc2_software_version
from custom_components.dahua.rpc2 import Rpc2MethodRefused

VTH_ANSWERS = {
    "magicBox.getDeviceClass": {"result": True, "params": {"type": "VTH"}},
    "magicBox.getDeviceType": {"result": True, "params": {"type": "VTH2421F-P"}},
    "magicBox.getSoftwareVersion": {
        "result": True,
        "params": {
            "version": {
                "Build": "20250123",
                "BuildDate": "2025-01-23",
                "Esn": "",
                "SecurityBaseLineVersion": "V2.4",
                "Version": "4.800.0000000.1.R",
                "WebVersion": "2.4",
            }
        },
    },
}


class _Rpc2:
    """Stands in for the shared RPC2 client and records what it was asked."""

    def __init__(self, answers):
        self.answers = answers
        self.asked = []

    async def request(self, method, params="unset", **kwargs):
        self.asked.append((method, params))
        answer = self.answers.get(method)
        if isinstance(answer, BaseException):
            raise answer
        if answer is None:
            raise Rpc2MethodRefused("not answered in this test")
        return answer


def _client(cgi_status=404, answers=None, shared_fails=None):
    """A client whose magicBox.cgi answers `cgi_status`, with a scripted RPC2 behind it."""
    client = DahuaClient("u", "p", "10.0.0.17", 80, 554, None)
    rpc2 = _Rpc2(VTH_ANSWERS if answers is None else answers)

    async def get(url, verify_ok=False):
        raise aiohttp.ClientResponseError(None, None, status=cgi_status)

    async def shared_call(action):
        if shared_fails is not None:
            raise shared_fails
        return await action(rpc2)

    client.get = get
    client._rpc2_shared_call = shared_call
    return client, rpc2


# --- a VTH is itself --------------------------------------------------------


async def test_the_model_is_the_vth_model():
    client, _ = _client()

    assert await client.get_device_type() == {"type": "VTH2421F-P"}


async def test_the_firmware_has_the_cgi_shape_so_the_build_date_still_parses():
    client, _ = _client()

    assert await client.get_software_version() == {
        "version": "4.800.0000000.1.R,build:2025-01-23"
    }


async def test_the_class_is_vth():
    client, _ = _client()

    assert await client.async_get_device_class() == "VTH"


async def test_nothing_is_recorded_as_invented():
    """The fallback warning says the identity had to be made up. It was not."""
    client, _ = _client()

    await client.get_device_type()
    await client.get_software_version()

    assert getattr(client, "_identity_fallbacks", {}) == {}


async def test_the_exact_questions_are_asked_once_for_all_three():
    """Setup asks for the version, the model and the class separately. One login's
    worth of questions, not three rounds of them."""
    client, rpc2 = _client()

    await client.get_software_version()
    await client.get_device_type()
    await client.async_get_device_class()

    assert rpc2.asked == [
        ("magicBox.getDeviceClass", None),
        ("magicBox.getDeviceType", None),
        ("magicBox.getSoftwareVersion", None),
        # Whether it has a camera of its own, see test_vth_without_video.py.
        ("configManager.getConfig", {"name": "RemoteDevice"}),
    ]


async def test_the_class_is_read_from_type_not_class():
    """RPC2 answers getDeviceClass in `type`, where the CGI uses `class`. A device
    answering in `class` over RPC2 has not been seen, so it is not a VTH here."""
    answers = dict(VTH_ANSWERS)
    answers["magicBox.getDeviceClass"] = {"result": True, "params": {"class": "VTH"}}
    client, _ = _client(answers=answers)

    assert await client.get_device_type() == {"type": "Generic RTSP"}


async def test_the_class_is_folded_like_the_cgi_one():
    answers = dict(VTH_ANSWERS)
    answers["magicBox.getDeviceClass"] = {"result": True, "params": {"type": " vth "}}
    client, _ = _client(answers=answers)

    assert await client.get_device_type() == {"type": "VTH2421F-P"}


async def test_a_501_is_an_absent_cgi_too():
    client, _ = _client(cgi_status=501)

    assert await client.get_device_type() == {"type": "VTH2421F-P"}


# --- everything else keeps the identity it had ---------------------------------


@pytest.mark.parametrize("device_class", ["VTO", "IPC", "NVR", "VTHX", ""])
async def test_a_device_that_is_not_a_vth_keeps_the_fallback(device_class):
    """A CGI-less VTO or camera running as `Generic RTSP` must not change shape.
    `VTHX` is the neighbouring-but-wrong answer."""
    answers = dict(VTH_ANSWERS)
    answers["magicBox.getDeviceClass"] = {
        "result": True,
        "params": {"type": device_class},
    }
    client, rpc2 = _client(answers=answers)

    assert await client.get_device_type() == {"type": "Generic RTSP"}
    assert await client.get_software_version() == {"version": "1.0"}
    assert [method for method, _ in rpc2.asked] == [
        "magicBox.getDeviceClass"
    ], "asked a non-VTH for its model"


async def test_a_device_that_is_not_a_vth_still_refuses_its_class():
    """The coordinator records that refusal; turning it into an answer would hide it."""
    answers = dict(VTH_ANSWERS)
    answers["magicBox.getDeviceClass"] = {"result": True, "params": {"type": "VTO"}}
    client, _ = _client(answers=answers)

    with pytest.raises(aiohttp.ClientResponseError):
        await client.async_get_device_class()


@pytest.mark.parametrize("status", [400, 401, 403, 500])
async def test_only_an_absent_cgi_is_asked_over_rpc2(status):
    """A 401 is the credentials, and an RPC2 login with them is one more failed login
    towards the device's lockout. A 400 is the device declining the action."""
    client, rpc2 = _client(cgi_status=status)

    assert await client.get_device_type() == {"type": "Generic RTSP"}
    assert await client.get_software_version() == {"version": "1.0"}
    with pytest.raises(aiohttp.ClientResponseError):
        await client.async_get_device_class()
    assert rpc2.asked == []


async def test_the_fallback_is_still_recorded_when_rpc2_has_no_vth():
    answers = dict(VTH_ANSWERS)
    answers["magicBox.getDeviceClass"] = {"result": True, "params": {"type": "IPC"}}
    client, _ = _client(answers=answers)

    await client.get_device_type()

    assert client._identity_fallbacks == {"getDeviceType": 404}


@pytest.mark.parametrize(
    "failure",
    [
        ConnectionError("no RPC2 here"),
        aiohttp.ClientConnectionError("refused"),
        Rpc2MethodRefused("refused", code=268894209),
        TimeoutError(),
    ],
)
async def test_no_rpc2_answer_falls_back_without_raising(failure):
    client, _ = _client(shared_fails=failure)

    assert await client.get_device_type() == {"type": "Generic RTSP"}
    assert await client.get_software_version() == {"version": "1.0"}


async def test_a_failed_attempt_is_not_repeated_for_every_question():
    """Remembered as None, so a device without RPC2 costs one attempt, not three."""
    client, rpc2 = _client(answers={})

    await client.get_device_type()
    await client.get_software_version()
    with pytest.raises(aiohttp.ClientResponseError):
        await client.async_get_device_class()

    assert rpc2.asked == [("magicBox.getDeviceClass", None)]


@pytest.mark.parametrize(
    "missing", ["magicBox.getDeviceType", "magicBox.getSoftwareVersion"]
)
async def test_a_vth_that_does_not_answer_everything_keeps_the_fallback(missing):
    """Half an identity would put a real model next to firmware `1.0`, or the reverse."""
    answers = dict(VTH_ANSWERS)
    del answers[missing]
    client, _ = _client(answers=answers)

    assert await client.get_device_type() == {"type": "Generic RTSP"}
    assert await client.get_software_version() == {"version": "1.0"}


@pytest.mark.parametrize(
    "method, answer",
    [
        ("magicBox.getDeviceType", {"result": True, "params": {"type": ""}}),
        ("magicBox.getDeviceType", {"result": True, "params": {}}),
        (
            "magicBox.getSoftwareVersion",
            {"result": True, "params": {"version": {"Build": "20250123"}}},
        ),
        (
            "magicBox.getSoftwareVersion",
            {"result": True, "params": {"version": "4.800"}},
        ),
    ],
)
async def test_a_vth_that_answers_with_nothing_usable_keeps_the_fallback(
    method, answer
):
    """Answered, not refused: the device said something, and it was not an identity."""
    answers = dict(VTH_ANSWERS)
    answers[method] = answer
    client, _ = _client(answers=answers)

    assert await client.get_device_type() == {"type": "Generic RTSP"}
    assert await client.get_software_version() == {"version": "1.0"}


async def test_a_working_cgi_is_never_asked_over_rpc2():
    client, rpc2 = _client()

    async def get(url, verify_ok=False):
        return {"type": "VTH2421F-P"}

    client.get = get

    await client.get_device_type()
    assert rpc2.asked == []


# --- the version's shape -------------------------------------------------------


@pytest.mark.parametrize(
    "params, expected",
    [
        (
            {"version": {"Version": "4.800.0000000.1.R", "BuildDate": "2025-01-23"}},
            "4.800.0000000.1.R,build:2025-01-23",
        ),
        (
            {"version": {"Version": "4.810.0000000.0.R", "BuildDate": "2025-07-07"}},
            "4.810.0000000.0.R,build:2025-07-07",
        ),
        (
            {"version": {"Version": " 4.800.0000000.1.R ", "BuildDate": ""}},
            "4.800.0000000.1.R",
        ),
        ({"version": {"Version": "4.800.0000000.1.R"}}, "4.800.0000000.1.R"),
        ({"version": {"Version": ""}}, None),
        ({"version": {"Build": "20250123"}}, None),
        ({"version": "4.800.0000000.1.R"}, None),  # not the measured shape
        ({}, None),
        (None, None),
    ],
)
def test_the_rpc2_version_is_put_in_the_cgi_shape(params, expected):
    assert rpc2_software_version(params) == expected
