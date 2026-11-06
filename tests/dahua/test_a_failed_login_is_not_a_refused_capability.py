"""A login the device declines is not the device declining a capability.

Every RPC2 read logs in first, so the login and the call arrive at the caller as
the same exception type. Thirteen `except Rpc2MethodRefused` handlers share that
blind spot; the one with a user-visible sentence attached reported the wrong
thing.

From @Masterz69's log on https://github.com/rroller/dahua/issues/1159, with the
method name left in:

    192.168.1.41 refused the siren and white light state over RPC2 (Dahua RPC2
    method global.login returned result=false (code=268632064,
    message=InterfaceNotFound)). That is this device saying it does not serve
    that call, not a fault

The refused method is `global.login`. The device said nothing whatever about
`CoaxialControlIO.getStatus`, and the line told the user it had. Worse, the
report is latched once per device, so the slot was spent on the wrong sentence
and a later genuine refusal of the siren read could never be printed.

**What this does not change is the poll.** Holding the previous reading on a
declined login is the right behaviour and #1006 is why the gather must not see
an exception here. Only the sentence and the latch were wrong.
"""

import json

import aiohttp
import pytest

import custom_components.dahua as dahua
from custom_components.dahua.coordinator import MAX_CAPABILITY_RETRIES
from custom_components.dahua.client import (
    DahuaClient,
    rpc2_refusal_is_a_failed_login,
    rpc2_refusal_is_a_stale_login,
)
from custom_components.dahua.rpc2 import (
    RPC2_LOGIN_METHOD,
    DahuaRpc2Client,
    Rpc2MethodRefused,
)
import custom_components.dahua.client as client_module

ADDRESS = "192.168.1.41"
DEVICE = "192.168.1.41:80"

# The code and words this camera answered its login with. Not a session code and
# not the word "session", which is why the stale-login question says no to it.
MEASURED_CODE = 268632064
MEASURED_MESSAGE = "InterfaceNotFound"


@pytest.fixture(autouse=True)
def _clear_shared_state():
    """Both of these are module level, so they outlive a test under `-n auto`."""
    dahua._CAPABILITY_REFUSALS_REPORTED.clear()
    client_module._HOST_RPC2.clear()
    yield
    dahua._CAPABILITY_REFUSALS_REPORTED.clear()
    client_module._HOST_RPC2.clear()


class _Reply:
    def __init__(self, payload):
        self.payload = payload

    async def text(self):
        return json.dumps(self.payload)


class _Http:
    """An HTTP layer that answers the challenge and then declines the login.

    Shaped on the real two-call login: the first `global.login` is sent with
    `verify_result=False` to collect the realm and the challenge, so only the
    second one can be refused. Driving the real `login()` rather than raising a
    ready-made exception is the point of this double, because the attribute
    under test is set three layers below the handler that reads it.
    """

    def __init__(self, code=MEASURED_CODE, message=MEASURED_MESSAGE, refuse=True):
        self.methods = []
        self._refuse = refuse
        self._code = code
        self._message = message
        self._challenge = {
            "result": False,
            "session": 777,
            "params": {"realm": "realm", "random": "challenge"},
        }

    async def post(self, url, json):
        self.methods.append(json["method"])
        # One login is two posts: the challenge, then the real attempt. Counted
        # per pair rather than from the start of the test, because a declined
        # login clears the shared session and the next poll logs in again.
        if len(self.methods) % 2 == 1:
            return _Reply(self._challenge)
        if self._refuse:
            return _Reply(
                {
                    "result": False,
                    "error": {"code": self._code, "message": self._message},
                }
            )
        return _Reply({"result": True, "session": "ok"})


def _rpc2(http):
    return DahuaRpc2Client("admin", "pw", ADDRESS, 80, 554, http)


# --- the enabler: a refusal now says what was refused ------------------------


async def test_a_declined_login_names_the_login_as_the_refused_method():
    """Driven through the real `login()`, because `method=method` being dropped
    anywhere between the raise site and the handler is the failure this guards."""
    with pytest.raises(Rpc2MethodRefused) as refused:
        await _rpc2(_Http()).login()

    assert refused.value.method == RPC2_LOGIN_METHOD
    assert refused.value.code == MEASURED_CODE
    assert refused.value.message == MEASURED_MESSAGE


async def test_a_declined_call_names_the_call_and_not_the_login():
    """The other half of the distinction. Without this a helper that always
    answered True would pass the test above."""
    http = _Http(refuse=False)
    client = _rpc2(http)
    await client.login()
    http._refuse = True

    with pytest.raises(Rpc2MethodRefused) as refused:
        await client.request("CoaxialControlIO.getStatus")

    assert refused.value.method == "CoaxialControlIO.getStatus"
    assert not rpc2_refusal_is_a_failed_login(refused.value)


async def test_the_login_method_is_named_once():
    """The handler compares against the string the client itself sends, so the
    two must be the same object of truth rather than two copies."""
    http = _Http()
    with pytest.raises(Rpc2MethodRefused):
        await _rpc2(http).login()

    assert http.methods == [RPC2_LOGIN_METHOD, RPC2_LOGIN_METHOD]


# --- the question a handler can now ask --------------------------------------


def test_the_helper_tells_a_declined_login_from_a_declined_call():
    login = Rpc2MethodRefused("no", method=RPC2_LOGIN_METHOD)
    call = Rpc2MethodRefused("no", method="CoaxialControlIO.getStatus")

    assert rpc2_refusal_is_a_failed_login(login)
    assert not rpc2_refusal_is_a_failed_login(call)


def test_a_refusal_with_no_method_is_not_read_as_a_login():
    """Thirteen handlers catch this type and the attribute is new, so a refusal
    raised by anything that does not set it must not start looking like a login
    failure. The class default is what makes the plain attribute read safe."""
    assert Rpc2MethodRefused.method is None
    assert not rpc2_refusal_is_a_failed_login(Rpc2MethodRefused("no"))
    assert not rpc2_refusal_is_a_failed_login(
        Rpc2MethodRefused("no", code=MEASURED_CODE, message=MEASURED_MESSAGE)
    )


def test_the_two_login_questions_are_not_the_same_question():
    """A stale session is worth a retry and a declined login is not, so the two
    must not both claim the measured refusal."""
    declined = Rpc2MethodRefused(
        "no", code=MEASURED_CODE, message=MEASURED_MESSAGE, method=RPC2_LOGIN_METHOD
    )

    assert rpc2_refusal_is_a_failed_login(declined)
    assert not rpc2_refusal_is_a_stale_login(declined)


# --- what the coordinator now says, end to end -------------------------------


def _coordinator(http):
    """A coordinator whose siren read goes through a real client and a real login.

    `_new_rpc2_session` is the only seam: everything from `_shared_rpc2` down,
    including `_rpc2_shared_call`'s re-raise, is the shipped code.
    """
    client = DahuaClient("admin", "pw", ADDRESS, 80, 554, None, False)
    client._new_rpc2_session = lambda: http

    coordinator = object.__new__(dahua.DahuaDataUpdateCoordinator)
    coordinator._address = ADDRESS
    coordinator.client = client
    coordinator.data = {
        "status.status.Speaker": "On",
        "status.status.WhiteLight": "Off",
    }
    coordinator.uses_rpc2_deterrence = lambda: True
    return coordinator


async def test_a_declined_login_is_not_reported_as_a_refused_capability(caplog):
    coordinator = _coordinator(_Http())

    with caplog.at_level("WARNING"):
        await coordinator._async_coaxial_status(0)

    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1, warnings
    assert "did not accept an RPC2 login" in warnings[0]
    assert "does not serve that call" not in warnings[0], (
        "the sentence this issue is about: a declined login described as the "
        "device saying it has no siren"
    )


async def test_a_declined_login_still_holds_the_previous_reading():
    """#1006: the gather must not see an exception here, and the entities say
    they hold their last value."""
    coordinator = _coordinator(_Http())

    assert await coordinator._async_coaxial_status(0) == {
        "status.status.Speaker": "On",
        "status.status.WhiteLight": "Off",
    }


async def test_a_declined_login_leaves_the_capability_slot_unspent(caplog):
    """The latch is per device, so the wrong sentence used to take the slot and
    the accurate one could never be printed afterwards."""
    coordinator = _coordinator(_Http())
    await coordinator._async_coaxial_status(0)

    assert (DEVICE, "rpc2") not in dahua._CAPABILITY_REFUSALS_REPORTED
    assert (DEVICE, "rpc2-login") in dahua._CAPABILITY_REFUSALS_REPORTED

    # Now the device answers the login and refuses the call itself, which is the
    # sentence the user needs and used to be suppressed.
    async def refuse_the_call(channel=0):
        raise Rpc2MethodRefused(
            "no", code=268894210, message="Method not found!", method="unknown"
        )

    coordinator.client.async_get_coaxial_control_io_status_rpc2 = refuse_the_call
    caplog.clear()
    with caplog.at_level("WARNING"):
        await coordinator._async_coaxial_status(0)

    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1, warnings
    assert "does not serve that call" in warnings[0]


async def test_a_declined_login_says_so_once_per_device(caplog):
    coordinator = _coordinator(_Http())

    with caplog.at_level("WARNING"):
        await coordinator._async_coaxial_status(0)
        await coordinator._async_coaxial_status(0)

    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1, warnings


async def test_a_stale_session_is_still_asked_about_first_and_is_not_a_failure():
    """#1159's other line, `Invalid session in request data!`, arrives on
    `global.login` too, and the stale question must keep being asked first.

    The order is what this is about. A stale session and a declined login are
    both refusals of `global.login`, so whichever branch runs first claims both
    unless the narrower question goes first. That is why the two are told apart
    by the session code and the word "session" rather than by the method alone.

    Neither of them fails the refresh any more. The escape used to, and the
    reason given for it was wrong: measured both ways, two polls cost four login
    posts whichever branch takes the refusal, because `_shared_rpc2` clears
    `holder.task` in its own handler before re-raising. The session is rebuilt
    either way, so the escape bought only the `Error fetching dahua data: ...
    code 287637505` line in #1159's log.
    """
    coordinator = _coordinator(
        _Http(code=287637505, message="Invalid session in request data!")
    )
    coordinator.data = {"status.status.Speaker": "On"}

    held = await coordinator._async_coaxial_status(0)

    assert held == {"status.status.Speaker": "On"}, "the reading was not held"
    assert dahua._CAPABILITY_REFUSALS_REPORTED == {
        (DEVICE, "rpc2-session")
    }, "an expired session is reported as itself, not as a declined login"
    assert (
        coordinator._coaxial_status_over_rpc2 is False
    ), "the transport verdict is not something an expired session gets to make"
    assert coordinator._coaxial_status_rpc2_asked is False, (
        "nor is 'this channel has been asked': the session expired before the "
        "device answered anything"
    )


# --- the second opinion, and what a declined login costs it ------------------


async def test_a_declined_login_does_not_write_rpc2_off_for_the_channel():
    """The second opinion latched `_coaxial_status_rpc2_asked` before the call
    and only un-latched it for an expired session, so a declined login stopped
    that channel ever trying RPC2 again and logged "RPC2 refuses the coaxial
    status too" about a transport that had answered nothing."""
    coordinator = _coordinator(_Http())

    assert await coordinator._async_coaxial_status_second_opinion(0) is None
    assert (
        coordinator._coaxial_status_rpc2_asked is False
    ), "a declined login is not this channel's answer about RPC2"
    assert coordinator._coaxial_status_over_rpc2 is False


async def test_the_retry_is_bounded_and_the_bound_is_the_login_count():
    """Measured, because this is the only part with a cost attached.

    Seven channels refreshing together spend **one** declined login, not seven:
    `_shared_rpc2` registers the login future before awaiting it and the poll
    fans out with `asyncio.gather`. One per poll is still not free, though. At
    the 30 second scan interval of
    https://github.com/rroller/dahua/issues/1159 that would be ten inside the
    300 second window a Dahua device locks out after five failures in, so the
    asking stops rather than going on for the life of the process.
    """
    http = _Http()
    coordinator = _coordinator(http)

    for _poll in range(6):
        await coordinator._async_coaxial_status_second_opinion(0)

    # Two posts per login: the challenge, then the attempt that is declined.
    logins = len(http.methods) // 2
    assert (
        logins == MAX_CAPABILITY_RETRIES
    ), "six polls spent %s declined logins; the bound is %s" % (
        logins,
        MAX_CAPABILITY_RETRIES,
    )
    assert coordinator._coaxial_status_rpc2_asked is True, "the asking never stopped"


async def test_a_declined_login_is_not_logged_as_the_capability_being_refused(caplog):
    coordinator = _coordinator(_Http())

    with caplog.at_level("DEBUG"):
        await coordinator._async_coaxial_status_second_opinion(0)

    messages = " ".join(r.getMessage() for r in caplog.records)
    assert "would not accept a login" in messages
    assert (
        "refuses the coaxial status too" not in messages
    ), "RPC2 was never asked about the coaxial status, so it refused nothing"


async def test_a_stale_second_opinion_holds_the_reading_and_retries_next_poll():
    http = _Http(code=287637505, message="Invalid session in request data!")
    coordinator = _coordinator(http)
    coordinator.uses_rpc2_deterrence = lambda: False

    async def refuse_cgi(channel):
        assert channel == 0
        raise aiohttp.ClientResponseError(None, (), status=400)

    coordinator.client.async_get_coaxial_control_io_status = refuse_cgi

    for _ in range(2):
        assert await coordinator._async_coaxial_status(0) == coordinator.data
        assert coordinator._coaxial_status_rpc2_asked is False
        assert coordinator._coaxial_status_over_rpc2 is False

    assert http.methods == [RPC2_LOGIN_METHOD] * 4
