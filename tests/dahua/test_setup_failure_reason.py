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
their password is wrong checks their password -- #690, #527, #497 and #496 are
all somebody doing that while the log quietly said `ConnectionRefusedError`.

Only 401 and 403 are credentials. Everything else is the device not being where,
or not being what, we were told.
"""

import asyncio
import json
import pathlib
import ssl

import pytest
from aiohttp import ClientConnectorError, ClientResponseError

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
    """Ticking HTTPS against a camera that does not serve it, most often."""
    assert describe_setup_failure(ssl.SSLError("handshake failure")) == "ssl_error"


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
