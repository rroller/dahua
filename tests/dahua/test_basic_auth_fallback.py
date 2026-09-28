"""Firmware that only speaks Basic must not read as a wrong password.

#583. Two old cameras, an IPC-HDW4300S-V2 on 2014 firmware and an
IPC-HDW4300C on 2015, answer the CGI interface with a Basic challenge:

    curl -u  user:pw  .../magicBox.cgi?action=getSystemInfo   -> 200
    curl --digest -u  .../magicBox.cgi?action=getSystemInfo   -> 401
    WWW-Authenticate: Basic realm="Device_CGI"

_parse_401 only ever recognised a Digest challenge and returned None for
anything else, so request() handed the 401 straight back. Upstream that reads
as a refused credential, and the user gets a reauth prompt that cannot succeed
with credentials the web UI accepts.

Basic is only ever sent after the device has asked for it by name, because it
puts the password in a header in the clear.
"""
import base64

from custom_components.dahua.digest import BASIC, DigestAuth


class _Response:
    def __init__(self, status, www_authenticate=None):
        self.status = status
        self.headers = {}
        if www_authenticate is not None:
            self.headers["www-authenticate"] = www_authenticate
        self.closed = False

    def close(self):
        self.closed = True


class _Session:
    """Answers each request from a script, recording what it was sent."""

    def __init__(self, replies):
        self._replies = list(replies)
        self.sent = []

    async def request(self, method, url, headers=None, **kwargs):
        self.sent.append(dict(headers or {}))
        return self._replies.pop(0)


def _auth(session, state=None):
    return DigestAuth("admin", "secret", session, state if state is not None else {})


def _expected_basic():
    return "Basic " + base64.b64encode(b"admin:secret").decode("ascii")


# --- the failing case -------------------------------------------------------

async def test_a_basic_challenge_is_answered_rather_than_returned():
    session = _Session([
        _Response(401, 'Basic realm="Device_CGI"'),
        _Response(200),
    ])

    response = await _auth(session).request("GET", "http://d/cgi-bin/x.cgi")

    assert response.status == 200, "the 401 was handed back instead of answered"
    assert session.sent[1].get("AUTHORIZATION") == _expected_basic()


async def test_the_first_attempt_carries_no_credentials():
    """Basic is only sent once the device has asked for it."""
    session = _Session([
        _Response(401, 'Basic realm="Device_CGI"'),
        _Response(200),
    ])

    await _auth(session).request("GET", "http://d/cgi-bin/x.cgi")

    assert "AUTHORIZATION" not in session.sent[0]


async def test_the_choice_is_remembered_for_the_next_request():
    """Otherwise every call to this device pays a 401 first."""
    state = {}
    first = _Session([_Response(401, 'Basic realm="Device_CGI"'), _Response(200)])
    await _auth(first, state).request("GET", "http://d/cgi-bin/x.cgi")

    second = _Session([_Response(200)])
    await _auth(second, state).request("GET", "http://d/cgi-bin/y.cgi")

    assert state.get("scheme") == BASIC
    assert second.sent[0].get("AUTHORIZATION") == _expected_basic()


async def test_a_wrong_password_still_fails():
    """Switching scheme must not turn a real refusal into a loop."""
    session = _Session([
        _Response(401, 'Basic realm="Device_CGI"'),
        _Response(401, 'Basic realm="Device_CGI"'),
        _Response(401, 'Basic realm="Device_CGI"'),
    ])

    response = await _auth(session).request("GET", "http://d/cgi-bin/x.cgi")

    assert response.status == 401
    assert len(session.sent) <= 3, "it kept retrying a credential the device refuses"


# --- everything else must be untouched --------------------------------------

async def test_a_device_that_wants_digest_is_unaffected():
    challenge = ('Digest realm="Login to device", qop="auth", '
                 'nonce="abc123", opaque="xyz"')
    session = _Session([_Response(401, challenge), _Response(200)])

    response = await _auth(session).request("GET", "http://d/cgi-bin/x.cgi")

    assert response.status == 200
    sent = session.sent[1].get("AUTHORIZATION", "")
    assert sent.startswith("Digest "), "a digest device was switched to Basic"


async def test_a_401_with_no_challenge_is_still_returned():
    session = _Session([_Response(401)])

    response = await _auth(session).request("GET", "http://d/cgi-bin/x.cgi")

    assert response.status == 401


async def test_an_unknown_scheme_is_not_guessed_at():
    session = _Session([_Response(401, 'Negotiate')])

    response = await _auth(session).request("GET", "http://d/cgi-bin/x.cgi")

    assert response.status == 401
    assert "AUTHORIZATION" not in session.sent[0]


def test_the_header_is_exactly_what_rfc7617_asks_for():
    header = _auth(_Session([]))._build_basic_header()

    scheme, _, encoded = header.partition(" ")
    assert scheme == "Basic"
    assert base64.b64decode(encoded) == b"admin:secret"
