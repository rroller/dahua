"""Three places decided "is this refusal a stale login?" and they disagreed.

`_direct_coaxial_rpc2` matched the documented code *or* a message saying the
session was out of date, because it had met a device that answered in words. Both
of the RPC2 event poll's handlers matched only the code. So the same device,
refusing the same way, recovered on one path while the other turned the refusal
into a closed event stream and backed the host off for up to ten minutes.

What is deliberately *not* here is a second code number. 287637504 is the only
session code documented anywhere -- the third-party RPC2 reference lists it and
nothing else -- and the numbers circulating for other session states are not. A
guessed number would make refusals that mean something else look like a session
problem, which spends a login and buries the real reason. What a device says
about a session is evidence; what its code might have been is not.
"""

from custom_components.dahua.client import (
    RPC2_SESSION_EXPIRED_CODE,
    rpc2_refusal_is_a_stale_login,
)
from custom_components.dahua.rpc2 import Rpc2MethodRefused


def _refusal(code=None, message=None):
    return Rpc2MethodRefused("refused", code=code, message=message)


# --- what counts -------------------------------------------------------------

def test_the_documented_code_counts():
    assert rpc2_refusal_is_a_stale_login(_refusal(code=RPC2_SESSION_EXPIRED_CODE))


def test_the_wording_the_coaxial_path_already_knew_counts():
    """This was the only message match in the codebase, and it was in one of the
    three places. Losing it here would be a regression on #775."""
    assert rpc2_refusal_is_a_stale_login(
        _refusal(code=287637504, message="session is out of date"))


def test_a_session_message_counts_without_a_recognised_code():
    """The case the event poll could not recover from: the device explains the
    problem in the message and carries a code nothing knows."""
    assert rpc2_refusal_is_a_stale_login(
        _refusal(code=999, message="Invalid session in request data!"))


def test_the_wording_is_matched_whatever_its_case():
    assert rpc2_refusal_is_a_stale_login(_refusal(message="SESSION Invalid"))


# --- and what does not -------------------------------------------------------

def test_a_refusal_about_something_else_does_not_count():
    """Measured on a DHI-NVR5464 answering a 17KB setConfig (#823). Treating this
    as a stale login would throw the session away and try again, twice, and then
    report a session problem for a request that was simply too long."""
    assert not rpc2_refusal_is_a_stale_login(
        _refusal(code=287638033, message="Request length error!"))


def test_an_unknown_method_does_not_count():
    assert not rpc2_refusal_is_a_stale_login(
        _refusal(code=268632064, message="InterfaceNotFound"))


def test_a_refusal_that_gave_no_reason_does_not_count():
    """A refusal with nothing in it is not evidence of anything. Guessing "stale
    login" here would make the poller discard a working session and retry on
    every reason-free refusal a device ever gives."""
    assert not rpc2_refusal_is_a_stale_login(_refusal())


def test_a_non_string_message_does_not_raise():
    """The field is whatever the device put there, and a device that sends a
    number must not take the event poll down with an AttributeError."""
    assert not rpc2_refusal_is_a_stale_login(_refusal(message=12345))


def test_something_that_is_not_a_refusal_at_all_does_not_raise():
    """Called from an except block that catches a type, but the predicate is the
    kind of helper that gets reused; it should answer rather than explode."""
    assert not rpc2_refusal_is_a_stale_login(ValueError("nothing to do with it"))
