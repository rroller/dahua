"""Which failures mean "this device will never do that", and which do not.

The whole value of remembering a refusal rests on this one decision. Get it too
narrow and a control that can never work keeps asking on every press. Get it too
wide and one timeout silences a working control for the life of the process, which
is far worse: the user has no way to know why the button stopped responding.

Measured, and these are the three that count:

    DHI-NVR5464-16P-EI  every Lighting write      HTTP 403 "Authority:check failure."
    DHI-NVR5464-16P-EI  the same, over RPC2       errCode 285278249, same message
    AD410 (#942)        CoaxialControlIO.control  errCode 268894210 "Method not found!"

`refusals.py` imports nothing but the standard library on purpose, so this is
testable without Home Assistant and the decision can be mutated.
"""

import asyncio

import aiohttp
import pytest

from custom_components.dahua import refusals
from custom_components.dahua.rpc2 import Rpc2MethodRefused
from custom_components.dahua.refusals import (
    forget,
    is_refused,
    key_for,
    refusal_is_outright,
    remember,
)


class _Error(Exception):
    """Stands in for aiohttp and rpc2 errors, which are duck-typed on two fields."""

    def __init__(self, message="", status=None, code=None):
        super().__init__(message)
        if status is not None:
            self.status = status
        if code is not None:
            self.code = code


class _Coordinator:
    def __init__(self, address="192.168.0.213", channel=0):
        self._address = address
        self._channel = channel

    def get_address(self):
        return self._address

    def get_channel(self):
        return self._channel


@pytest.fixture(autouse=True)
def _clean():
    forget()
    yield
    forget()


# --- what counts --------------------------------------------------------------


def test_the_status_a_recorder_refuses_a_config_write_with():
    assert refusal_is_outright(_Error("Forbidden", status=403)) is True


def test_the_rpc2_code_for_the_same_answer():
    assert (
        refusal_is_outright(_Error("Authority:check failure.", code=285278249)) is True
    )


def test_a_method_the_device_does_not_have():
    """#942: an AD410 advertises a siren from two sources and then answers this to
    the call that operates it. A method either exists or it does not."""
    assert refusal_is_outright(_Error("Method not found!", code=268894210)) is True


# --- what deliberately does not ------------------------------------------------


@pytest.mark.parametrize(
    "error,why",
    [
        (
            _Error("Unauthorized", status=401),
            "the credentials, which reauth exists for; a later write may succeed",
        ),
        (_Error("Bad Request", status=400), "the request, not the capability"),
        (_Error("Internal Server Error", status=500), "the device having a bad moment"),
        (
            _Error("session is out of date!", code=287637504),
            "an expired session, which the shared-login retry already handles",
        ),
        (
            _Error("Unknown error! error code was not set in service!", code=268959743),
            "a device declining without saying why -- #943 has one answering this "
            "where it used to work, so it is not permanent",
        ),
        (asyncio.TimeoutError(), "never answered at all"),
        (ConnectionError("reset"), "never answered at all"),
        (_Error("something"), "no status and no code"),
    ],
)
def test_these_are_not_a_refusal(error, why):
    assert refusal_is_outright(error) is False, why


def test_an_odd_value_in_either_field_is_not_a_refusal():
    """Membership is the whole test, so nothing needs guarding against.

    This replaced a pair of assertions about `status=True`, which the mutation
    sweep showed could not fail: `True in {403}` is False whether or not the code
    checks the type first. The bool guard they were protecting was unreachable and
    has gone. `dahua_utils.describe_write_refusal` genuinely needs its one,
    because there a bool formats as "HTTP 1".
    """
    for value in (True, "403", None, object()):
        assert refusal_is_outright(_Error("x", status=value)) is False
        assert refusal_is_outright(_Error("x", code=value)) is False

    # `403.0` is deliberately not in that list: it equals 403 and hashes the same,
    # so it really is a member and the first draft of this test was simply wrong
    # about it. aiohttp gives an int, so nothing produces a float here -- but
    # asserting it would be false, and a test that lies is worse than no test.
    assert refusal_is_outright(_Error("x", status=403.0)) is True


# --- against the real exceptions, because the stand-in lied -------------------
#
# The first version of refusal_is_outright read `code` before `status`, on the
# reasoning that an Rpc2MethodRefused carries a code and nothing gives it a
# status. Every test above passed. CI failed, because
# `aiohttp.ClientResponseError` carries a **deprecated `code` attribute aliasing
# `status`** -- so a 403 was looked up among the RPC2 numbers, found absent, and
# reported as not a refusal. The hand-built `_Error` has no such alias, so it could
# not show it.
#
# These three use the real classes. They are the ones that would have caught it.


def test_a_real_aiohttp_403_is_a_refusal():
    error = aiohttp.ClientResponseError(
        aiohttp.RequestInfo(
            url="http://recorder/cgi-bin/configManager.cgi",
            method="GET",
            headers=aiohttp.typedefs.CIMultiDict(),
            real_url="http://recorder/cgi-bin/configManager.cgi",
        ),
        (),
        status=403,
        message="Forbidden",
    )

    assert refusal_is_outright(error) is True


def test_a_real_aiohttp_500_is_not():
    error = aiohttp.ClientResponseError(
        aiohttp.RequestInfo(
            url="http://recorder/cgi-bin/configManager.cgi",
            method="GET",
            headers=aiohttp.typedefs.CIMultiDict(),
            real_url="http://recorder/cgi-bin/configManager.cgi",
        ),
        (),
        status=500,
        message="Internal Server Error",
    )

    assert refusal_is_outright(error) is False


@pytest.mark.parametrize(
    "code,expected",
    [
        (285278249, True),
        (268894210, True),
        (287637504, False),
        (268959743, False),
    ],
)
def test_a_real_rpc2_refusal_is_judged_on_its_code(code, expected):
    """It has a `code` and no `status`, which is what makes the order work."""
    error = Rpc2MethodRefused("refused", code=code, message="whatever")

    assert not hasattr(error, "status"), (
        "if this ever grows a status, the order in refusal_is_outright needs "
        "rereading"
    )
    assert refusal_is_outright(error) is expected


# --- and what is remembered ----------------------------------------------------


def test_a_refusal_is_remembered_for_that_control_on_that_channel():
    coordinator = _Coordinator(channel=3)
    remember(coordinator, "siren", "errCode 268894210 Method not found!")

    assert is_refused(coordinator, "siren")
    assert not is_refused(
        coordinator, "infrared"
    ), "refusing the siren must not silence the infrared on the same channel"
    assert not is_refused(_Coordinator(channel=4), "siren")
    assert not is_refused(_Coordinator(address="192.168.0.232", channel=3), "siren")


def test_the_key_is_the_address_not_the_serial():
    """`_serial_number` is a bare annotation on the coordinator until the device
    answers, so `get_serial_number()` raises on one whose setup did not finish --
    and the unload hook meets exactly those. That took async_unload_entry down
    once already."""
    assert key_for(_Coordinator(channel=3), "siren") == ("192.168.0.213", 3, "siren")


def test_unload_forgets_every_control_on_that_channel_and_no_other():
    one = _Coordinator(channel=3)
    other = _Coordinator(channel=4)
    for coordinator in (one, other):
        for control in ("siren", "infrared"):
            remember(coordinator, control, "refused")

    forget(one)

    assert not is_refused(one, "siren") and not is_refused(one, "infrared")
    assert is_refused(other, "siren"), "another channel was forgotten too"
    assert is_refused(other, "infrared")


def test_forgetting_one_control_leaves_the_others():
    coordinator = _Coordinator(channel=3)
    remember(coordinator, "siren", "refused")
    remember(coordinator, "infrared", "refused")

    forget(coordinator, "siren")

    assert not is_refused(coordinator, "siren")
    assert is_refused(coordinator, "infrared")


def test_forgetting_everything_is_what_the_test_fixture_needs():
    remember(_Coordinator(), "siren", "refused")
    forget()
    assert not refusals._REFUSED


def test_a_refusal_is_announced_once_rather_than_per_press(caplog):
    """The log line is the only place a user finds out why the control went
    quiet, so it has to name the device, the control and the reason."""
    import logging

    with caplog.at_level(logging.WARNING):
        remember(
            _Coordinator(channel=3), "siren", "errCode 268894210 Method not found!"
        )

    assert "192.168.0.213" in caplog.text
    assert "siren" in caplog.text
    assert "268894210" in caplog.text
    assert "stop asking" in caplog.text
    assert "Reload the entry" in caplog.text
