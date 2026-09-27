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

from custom_components.dahua import config_flow
from custom_components.dahua.config_flow import describe_setup_failure


def _response_error(status):
    return ClientResponseError(None, None, status=status, message="x")


# --- the one case that really is the password --------------------------------

def test_a_rejected_login_is_a_rejected_login():
    assert describe_setup_failure(_response_error(401)) == "auth"


def test_a_403_is_not_a_rejected_login():
    """It used to be listed alongside 401, which contradicted the other half of the
    codebase. _is_login_refused is `status == 401` and says why:

        403 deliberately keeps the fallback. It means the login was accepted and
        this account is not allowed that endpoint, which a restricted Dahua user
        really can hit, and their credentials are not wrong.

    So a 403 never propagates out of the identity calls and cannot reach here. The
    entry is created with a synthesised id instead, which is the documented
    behaviour. Calling it a credentials failure was wrong even though it was
    unreachable, and unreachable wrong code is how a later change reintroduces a
    bug with confidence.
    """
    assert describe_setup_failure(_response_error(403)) != "auth"


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


@pytest.mark.parametrize("status", [400, 500, 503])
def test_something_that_answered_but_not_as_a_camera(status):
    assert describe_setup_failure(_response_error(status)) == "unexpected_reply"


def test_a_404_on_the_api_path_points_at_the_cgi_service():
    """404 used to be folded into "not a Dahua camera", which is the one reading
    that leads nowhere.

    Something is serving HTTP and does not have magicBox.cgi. On Dahua that is the
    CGI service switched off, and it is a checkbox: #145, #417 and #465 were all
    this, and #465 asked for this exact hint. A non-Dahua web server lands here
    too, which the message covers.
    """
    assert describe_setup_failure(_response_error(404)) == "cgi_disabled"


# --- what "nothing answered" was actually hiding -----------------------------
#
# cannot_connect says the request did not land, not why. Its message guesses, and
# in the two commonest cases it guesses wrong:
#
#   "Nothing answered at that address and port. Check the camera is on, that the
#    IP and port are right, and that Home Assistant can reach it."
#
# A device on HTTPS, and a device whose web service is off while Dahua's own
# protocols still answer, are both on, correctly addressed and reachable.


def _probe_opening(open_ports):
    async def _probe(address, port, timeout=None):
        return port in open_ports
    return _probe


async def test_a_dahua_port_answering_means_http_is_switched_off(monkeypatch):
    """Measured on three cameras here: 5000 and 37777 open, and nothing at all on
    any of 27 scanned HTTP ports."""
    monkeypatch.setattr(config_flow, "_async_probe_tcp", _probe_opening({37777}))

    assert await config_flow.async_refine_connection_failure(
        "1.2.3.4", "cannot_connect") == "http_service_off"


async def test_dhip_on_its_own_is_enough(monkeypatch):
    monkeypatch.setattr(config_flow, "_async_probe_tcp", _probe_opening({5000}))

    assert await config_flow.async_refine_connection_failure(
        "1.2.3.4", "cannot_connect") == "http_service_off"


async def test_443_open_suggests_https_and_takes_precedence(monkeypatch):
    """Of the two, HTTPS is the one with a port to hand the user."""
    monkeypatch.setattr(config_flow, "_async_probe_tcp",
                        _probe_opening({443, 37777}))

    assert await config_flow.async_refine_connection_failure(
        "1.2.3.4", "cannot_connect") == "https_available"


async def test_nothing_listening_stays_cannot_connect(monkeypatch):
    """The message is right when the device really is unreachable."""
    monkeypatch.setattr(config_flow, "_async_probe_tcp", _probe_opening(set()))

    assert await config_flow.async_refine_connection_failure(
        "1.2.3.4", "cannot_connect") == "cannot_connect"


async def test_a_failure_that_is_not_a_connection_failure_is_left_alone(monkeypatch):
    """A refused login must not turn into a port scan. Those messages are already
    right, and the device is one that locks out on repeated attention."""
    probed = []

    async def _record(address, port, timeout=None):
        probed.append(port)
        return True

    monkeypatch.setattr(config_flow, "_async_probe_tcp", _record)

    for reason in ("auth", "timeout", "ssl_error", "cgi_disabled",
                   "unexpected_reply", "unknown"):
        assert await config_flow.async_refine_connection_failure(
            "1.2.3.4", reason) == reason

    assert not probed, "nothing should be probed unless the connection failed"


def test_anything_unrecognised_points_at_the_log():
    assert describe_setup_failure(ValueError("something else")) == "unknown"


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

    produced.add(describe_setup_failure(_response_error(404)))

    missing = produced - set(strings)
    assert not missing, "no translation for %s" % sorted(missing)


def test_the_refined_reasons_have_translations_too():
    """These come from async_refine_connection_failure rather than from
    describe_setup_failure, so the test above cannot see them."""
    path = (pathlib.Path(__file__).parents[2]
            / "custom_components" / "dahua" / "translations" / "en.json")
    strings = json.loads(path.read_text(encoding="utf-8"))["config"]["error"]

    missing = {"https_available", "http_service_off", "cgi_disabled"} - set(strings)
    assert not missing, "no translation for %s" % sorted(missing)


def test_no_reason_still_blames_the_password_by_accident():
    """The old text is kept only for a genuine rejection, and says so now."""
    path = (pathlib.Path(__file__).parents[2]
            / "custom_components" / "dahua" / "translations" / "en.json")
    strings = json.loads(path.read_text(encoding="utf-8"))["config"]["error"]

    assert "Address" not in strings["auth"], (
        "the credentials message must not mention the address any more")
