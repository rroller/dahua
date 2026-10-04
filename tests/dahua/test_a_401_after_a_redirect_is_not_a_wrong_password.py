"""A 401 that only arrived after a redirect says nothing about the password.

#947. A DHI-NVR4108-8P-4KS2 with HTTPS switched on answers the CGI endpoint on
port 80 with a redirect rather than a challenge:

    HTTP/1.1 302 Moved Temporarily
    Location: https://192.168.178.54:443/cgi-bin/magicBox.cgi?action=getMachineName

aiohttp follows it. The digest exchange does not survive the change of scheme and
port, so the device answers the second request unauthenticated, and the 401 that
comes back was classified as `auth`.

That is wrong twice over. The credentials were never tested, and because `auth` is
in TRANSPORT_WORKED the port and HTTPS fields stay hidden -- the two fields that fix
it. The reporter spent the detour switching HTTPS *off* on the NVR to get past the
add form, then turned it back on and let the integration's own repair move the entry
to 443.

Which is the other half of it: the integration already detects this once an entry
exists, as `http_dead_https_available`. It just never asked during setup.

Nothing here probes the network. aiohttp has already recorded what happened:
`raise_for_status` passes the response's redirect history, and `request_info` carries
the URL the final request actually went to.
"""

import json
import pathlib

import pytest
from aiohttp import ClientResponseError
from yarl import URL

from custom_components.dahua.config_flow import (
    TRANSPORT_WORKED,
    describe_setup_failure,
    redirected_to_https,
)


class _Redirect:
    """One hop of aiohttp's `history`, which holds responses and not URLs."""

    def __init__(self, status=302):
        self.status = status


class _RequestInfo:
    def __init__(self, url):
        self.url = URL(url)


def _error(status=401, history=(), final=None):
    """A ClientResponseError shaped the way raise_for_status builds one."""
    info = _RequestInfo(final) if final else None
    return ClientResponseError(info, tuple(history), status=status, message="x")


FINAL_HTTPS = "https://192.168.178.54:443/cgi-bin/magicBox.cgi?action=getMachineName"
FINAL_HTTP = "http://192.168.178.54:80/cgi-bin/magicBox.cgi?action=getMachineName"


# --- the reported case --------------------------------------------------------


def test_the_reported_401_is_reported_as_a_redirect():
    reason = describe_setup_failure(_error(401, [_Redirect(302)], FINAL_HTTPS))

    assert reason == "https_redirect"


def test_and_that_reason_reveals_the_fields_that_fix_it():
    """The whole point. `auth` is in TRANSPORT_WORKED, so the port and HTTPS
    fields stay hidden, and they are the only way out of this."""
    assert "https_redirect" not in TRANSPORT_WORKED


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_every_redirect_status_counts(status):
    """Dahua sends 302 here, but nothing should depend on which one."""
    assert redirected_to_https(_error(401, [_Redirect(status)], FINAL_HTTPS)) is True


def test_a_chain_of_hops_still_counts():
    assert (
        redirected_to_https(_error(401, [_Redirect(302), _Redirect(307)], FINAL_HTTPS))
        is True
    )


# --- and a real refused password must keep saying so --------------------------


def test_a_401_with_no_redirect_is_still_the_password():
    """The common case, and the one this must not swallow: a wrong password on a
    device that answered directly."""
    assert describe_setup_failure(_error(401, (), FINAL_HTTP)) == "auth"


def test_a_401_with_no_history_at_all_is_still_the_password():
    """Most of this suite builds the exception with no history, and a device that
    challenges directly has none."""
    assert describe_setup_failure(_error(401)) == "auth"


def test_a_device_already_on_https_can_still_refuse_a_password():
    """The case a mutation found missing here, and the one that matters most to
    anybody already set up correctly.

    An entry on port 443 that gets a wrong password produces a 401 whose final URL
    is https and whose history is empty. Deciding by the scheme alone, or defaulting
    an absent history to "there was a redirect", would tell them to tick a box they
    ticked long ago and never mention their password.
    """
    assert redirected_to_https(_error(401, (), FINAL_HTTPS)) is False
    assert describe_setup_failure(_error(401, (), FINAL_HTTPS)) == "auth"


def test_a_redirect_that_stayed_on_http_is_not_this():
    """A device can redirect within HTTP, to a different path or port. That is not
    evidence about HTTPS and must not send somebody to tick the box."""
    assert redirected_to_https(_error(401, [_Redirect(302)], FINAL_HTTP)) is False
    assert describe_setup_failure(_error(401, [_Redirect(302)], FINAL_HTTP)) == "auth"


def test_history_that_holds_no_redirect_is_not_this():
    """`history` is a tuple of responses, and a non-redirect in it would mean
    something other than a hop. Reading it as one would report a redirect that
    never happened."""
    assert redirected_to_https(_error(401, [_Redirect(401)], FINAL_HTTPS)) is False


def test_a_missing_request_info_does_not_raise():
    """Nothing guarantees aiohttp filled it in, and a crash in the function that
    explains a failure would replace the explanation with a traceback."""
    assert redirected_to_https(_error(401, [_Redirect(302)], None)) is False


def test_an_exception_that_is_not_a_response_error_is_untouched():
    assert redirected_to_https(OSError("boom")) is False


# --- the other statuses are unaffected ----------------------------------------


def test_a_404_is_still_the_cgi_service():
    assert (
        describe_setup_failure(_error(404, [_Redirect(302)], FINAL_HTTPS))
        == "cgi_disabled"
    )


def test_another_status_is_still_an_unexpected_reply():
    assert (
        describe_setup_failure(_error(500, [_Redirect(302)], FINAL_HTTPS))
        == "unexpected_reply"
    )


# --- and the user actually sees a sentence ------------------------------------


def test_the_reason_has_an_english_message():
    """English is the per-key fallback, so a missing string here is a bare key in
    every language."""
    path = (
        pathlib.Path(__file__).parents[2]
        / "custom_components"
        / "dahua"
        / "translations"
        / "en.json"
    )
    errors = json.loads(path.read_text(encoding="utf-8"))["config"]["error"]

    assert "https_redirect" in errors
    message = errors["https_redirect"]
    assert "443" in message, "it has to name the port to use"
    assert "HTTPS" in message
