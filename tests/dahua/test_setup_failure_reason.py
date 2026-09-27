"""Say why a camera could not be added, rather than blaming the password.

Adding a camera ran one check and reported one error:

    data = await self._test_credentials(...)
    if data is not None:
        ...
    else:
        self._errors["base"] = "auth"   # "Username, Password, or Address is wrong."

and `_test_credentials` caught every exception. So a device that refuses the
connection, one on the wrong port, one that wants HTTPS, and one that is simply
switched off all produced the same sentence about credentials. A person told
their password is wrong checks their password -- #690 is somebody doing that
while the log quietly said `ConnectionRefusedError`.

Only 401 and 403 are credentials. Everything else is the device not being where,
or not being what, we were told.
"""

import asyncio
import json
import pathlib
import ssl

import pytest
from aiohttp import (ClientConnectorCertificateError, ClientConnectorError,
                     ClientConnectorSSLError, ClientResponseError, ClientSSLError)

from custom_components.dahua.config_flow import describe_setup_failure


def _response_error(status):
    return ClientResponseError(None, None, status=status, message="x")


# --- the one case that really is the password --------------------------------

@pytest.mark.parametrize("status", [401, 403])
def test_a_rejected_login_is_a_rejected_login(status):
    assert describe_setup_failure(_response_error(status)) == "auth"


# --- and the ones that never were --------------------------------------------

def test_a_refused_connection_says_so():
    """#690: the log said ConnectionRefusedError; the form said check your password."""
    err = ClientConnectorError(connection_key=None, os_error=OSError(111, "refused"))

    assert describe_setup_failure(err) == "cannot_connect"


def test_a_bare_os_error_is_also_a_connection_problem():
    """Not every refusal arrives wrapped."""
    assert describe_setup_failure(ConnectionRefusedError(111, "refused")) == "cannot_connect"
    assert describe_setup_failure(OSError(113, "no route to host")) == "cannot_connect"


def test_a_timeout_is_a_timeout():
    assert describe_setup_failure(TimeoutError()) == "timeout"
    assert describe_setup_failure(asyncio.TimeoutError()) == "timeout"


def test_an_ssl_failure_names_https():
    """Ticking HTTPS against a camera that does not serve it, most often.

    A bare ssl.SSLError only arrives from something outside a request. Keep this,
    but note it is not the shape the flow actually sees -- see below.
    """
    assert describe_setup_failure(ssl.SSLError("handshake failure")) == "ssl_error"


# --- the TLS failure a request actually raises -------------------------------
#
# This is what was broken. `ssl_error` was written and translated, and no user
# could ever see it, because the branch above it matched first.

def test_the_tls_error_a_request_raises_names_https():
    """aiohttp raises ClientConnectorSSLError, never a bare ssl.SSLError.

    The test above passed throughout, which is exactly why this one is needed:
    it asserted on a type nothing in the flow produces.
    """
    err = ClientConnectorSSLError(
        connection_key=None, os_error=ssl.SSLError("handshake failure"))

    assert describe_setup_failure(err) == "ssl_error"


def test_a_self_signed_certificate_names_https():
    """#248 and #314: the DVR's own certificate. Reported as a credentials
    problem for years, because this landed on cannot_connect."""
    err = ClientConnectorCertificateError(
        connection_key=None, certificate_error=ssl.CertificateError("self signed"))

    assert describe_setup_failure(err) == "ssl_error"


def test_the_branch_order_is_load_bearing():
    """Why the TLS test must come before the connection test.

    If this ever fails, aiohttp changed its hierarchy and the ordering comment in
    describe_setup_failure needs rereading -- not this test deleting.
    """
    assert issubclass(ClientSSLError, ClientConnectorError), (
        "a TLS error IS a connection error, so testing ClientConnectorError "
        "first makes ssl_error unreachable")
    assert issubclass(ClientConnectorSSLError, ClientSSLError)
    assert issubclass(ClientConnectorCertificateError, ClientSSLError)


def test_why_the_clientsslerror_arm_is_belt_and_braces():
    """Naming ClientSSLError as well as ssl.SSLError is deliberate, and today it
    is redundant. Recording that here rather than leaving it looking load-bearing.

    Every TLS error aiohttp raises is *also* an ssl.SSLError, so ordering alone is
    what fixes this and the extra arm changes no outcome. It is kept because it
    says what the branch is for, and because it is the thing that would still hold
    if aiohttp ever added a TLS error outside the ssl hierarchy.

    If either assertion below stops holding, that arm has become load-bearing and
    the comment in describe_setup_failure needs updating to say so.
    """
    assert issubclass(ClientConnectorSSLError, ssl.SSLError)
    assert issubclass(ClientConnectorCertificateError, ssl.SSLError)


@pytest.mark.parametrize("status", [400, 404, 500, 503])
def test_something_that_answered_but_not_as_a_camera(status):
    assert describe_setup_failure(_response_error(status)) == "unexpected_reply"


def test_anything_unrecognised_points_at_the_log():
    assert describe_setup_failure(ValueError("something else")) == "unknown"


# --- every reason must be a string a user can actually read ------------------

def test_every_reason_has_a_translation():
    """A key with no string shows the raw key in the UI."""
    path = (pathlib.Path(__file__).parents[2]
            / "custom_components" / "dahua" / "translations" / "en.json")
    strings = json.loads(path.read_text(encoding="utf-8"))["config"]["error"]

    produced = {
        describe_setup_failure(_response_error(401)),
        describe_setup_failure(_response_error(500)),
        describe_setup_failure(ConnectionRefusedError()),
        describe_setup_failure(TimeoutError()),
        describe_setup_failure(ssl.SSLError()),
        describe_setup_failure(ClientConnectorSSLError(
            connection_key=None, os_error=ssl.SSLError())),
        describe_setup_failure(ValueError()),
    }

    missing = produced - set(strings)
    assert not missing, "no translation for %s" % sorted(missing)


def test_no_reason_still_blames_the_password_by_accident():
    """The old text is kept only for a genuine rejection, and says so now."""
    path = (pathlib.Path(__file__).parents[2]
            / "custom_components" / "dahua" / "translations" / "en.json")
    strings = json.loads(path.read_text(encoding="utf-8"))["config"]["error"]

    assert "Address" not in strings["auth"], (
        "the credentials message must not mention the address any more")
