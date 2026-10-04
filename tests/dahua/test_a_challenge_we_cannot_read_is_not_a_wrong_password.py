"""A challenge this integration cannot read must not be reported as a bad password.

#947: a DHI-NVR4108-8P-4KS2 on 2021 firmware, reached over HTTPS, could not be
added. The reporter had already checked the credentials in a browser against the
same CGI endpoint and got an answer back. What they were told was:

    "The camera rejected that username and password"
    aiohttp.client_exceptions.ClientResponseError: 401, message='Unauthorized',
    url='https://192.168.178.54/cgi-bin/magicBox.cgi?action=getMachineName'

Every 401 that reaches a caller is reported that way, and nothing recorded what the
device had actually asked for, so a refused password and an unreadable challenge
were indistinguishable. Probing the committed module with challenges real firmware
sends found three ways to produce that message without the password being wrong:

    WWW-Authenticate                                          before   after
    Digest realm="Login to 4KS2", qop="auth", nonce="a"        added    added
    Digest realm="Login to 4KS2", qop="auth,auth-int", ...     401      added
    Digest realm="Login to 4KS2", qop="auth, auth-int", ...    401      added
    Digest realm="Login to device, channel 1", nonce="a"       401      added
    Digest ..., algorithm=SHA-256                              401      added
    Digest ..., algorithm=SHA-256-sess                         401      added
    Digest ..., algorithm=SHA-512-256                          401      401, and says so

1. **The parser split on every comma.** `parse_key_value_list` did
   `header.split(",")`, so a comma inside a quoted value cut the value in half. The
   fragment had no `=` in it, `parse_pair` raised ValueError, and `_parse_401` read
   that as "this 401 carries no digest challenge" -- so the request went back out
   with no credentials until the attempt budget ran out. `qop="auth,auth-int"` is
   RFC 7616's ordinary way to offer both, and the realm is free text.

2. **The SHA-256 family signed nothing.** `_build_digest_header` knew MD5, MD5-SESS
   and SHA, and returned `""` for everything else. An empty header is not an error
   anywhere: `request()` drops the challenge and probes again, three times, and
   hands back the last 401.

3. **Nothing said so.** Which is what made the other two arrive as a model number
   and a traceback with no way to tell them apart.

SHA-512-256 stays unimplemented on purpose. RFC 7616 names NIST's truncated
variant, which is not SHA-512 cut to 256 bits, and hashlib only reaches it through
an OpenSSL name. A wrong signature would be refused just the same, so the honest
answer is the absent credential plus a log line naming the algorithm.
"""

import hashlib
import logging
import re

import pytest

from custom_components.dahua.digest import (
    ANSWERABLE_SCHEMES,
    BASIC,
    DIGEST,
    DigestAuth,
    parse_key_value_list,
    parse_pair,
    split_header_fields,
)

USER = "admin"
PASSWORD = "secret"
REALM = "Login to 4KS2"
URL = "/cgi-bin/magicBox.cgi?action=getMachineName"


class _Response:
    def __init__(self, status, www_authenticate=None):
        self.status = status
        self.headers = {}
        if www_authenticate is not None:
            self.headers["www-authenticate"] = www_authenticate

    def close(self):
        pass


class _Device:
    """Answers 401 with one challenge until credentials show up, then 200."""

    def __init__(self, challenge):
        self.challenge = challenge
        self.sent = []

    async def request(self, method, url, headers=None, **kwargs):
        authorization = (headers or {}).get("AUTHORIZATION")
        self.sent.append(authorization)
        if authorization:
            return _Response(200)
        return _Response(401, self.challenge)


def _auth(session=None, state=None):
    return DigestAuth(USER, PASSWORD, session, {} if state is None else state)


def _for_challenge(challenge):
    auth = _auth()
    auth.challenge = dict(challenge)
    return auth


def _params(header):
    body = header.partition(" ")[2]
    return {
        m[0]: (m[1] or m[2]) for m in re.findall(r'(\w+)=(?:"([^"]*)"|([^,\s]+))', body)
    }


def _warnings(caplog):
    """Warnings from this integration only.

    caplog collects every logger in the process, so counting by level alone would
    make these assertions hostage to anything else that happens to warn.
    """
    return [
        r
        for r in caplog.records
        if r.levelno >= logging.WARNING and r.name == "custom_components.dahua"
    ]


def _logged(caplog):
    return "\n".join(
        r.getMessage() for r in caplog.records if r.name == "custom_components.dahua"
    )


def _md5(value):
    return hashlib.md5(value.encode()).hexdigest()


def _sha256(value):
    return hashlib.sha256(value.encode()).hexdigest()


# --- 1. the comma inside a quoted value -----------------------------------------


@pytest.mark.parametrize(
    "header",
    [
        'Digest realm="%s", qop="auth,auth-int", nonce="abc"' % REALM,
        'Digest realm="%s", qop="auth, auth-int", nonce="abc"' % REALM,
        'Digest realm="Login to device, channel 1", qop="auth", nonce="abc"',
        'Digest qop="auth,auth-int", realm="Login to device, channel 1", nonce="abc"',
    ],
)
async def test_a_comma_inside_a_quoted_value_does_not_destroy_the_challenge(header):
    """The #947 fault. Each of these used to end as a 401 the user was told was a
    wrong username and password."""
    device = _Device(header)

    response = await _auth(device).request("GET", URL)

    assert response.status == 200, "the challenge was discarded: %s" % header
    assert device.sent[1], "the second attempt went out with no credentials"


def test_the_two_value_qop_survives_parsing_intact():
    """Not just "it parsed": the value has to arrive whole, because
    _build_digest_header checks `auth` against the list and signs `qop="auth"`."""
    fields = parse_key_value_list(
        'realm="%s", qop="auth,auth-int", nonce="abc"' % REALM
    )

    assert fields == {"realm": REALM, "qop": "auth,auth-int", "nonce": "abc"}


def test_a_realm_containing_a_comma_arrives_whole():
    """The realm goes into HA1. Half a realm signs a digest the device refuses,
    which is the same 401 by another route."""
    fields = parse_key_value_list('realm="Login to device, channel 1", nonce="abc"')

    assert fields["realm"] == "Login to device, channel 1"


def test_a_qop_list_with_spaces_is_still_recognised_as_offering_auth():
    """`qop="auth, auth-int"` is the same offer. Without stripping the parts, the
    membership test looked for "auth" in [" auth-int", "auth"] -- which happens to
    work in that order and not in the other, so the header's field order decided
    whether the device could be used."""
    auth = _for_challenge({"realm": REALM, "nonce": "n1", "qop": "auth-int, auth"})

    assert _params(auth._build_digest_header("GET", URL))["qop"] == "auth"


def test_a_qop_this_cannot_do_is_still_refused_when_it_is_the_only_one_offered():
    """The tolerance above must not turn into accepting auth-int alone."""
    from aiohttp.client_exceptions import ClientError

    auth = _for_challenge({"realm": REALM, "nonce": "n1", "qop": " auth-int "})

    with pytest.raises(ClientError):
        auth._build_digest_header("GET", URL)


# --- how the splitter behaves, which is the load-bearing part ------------------


def test_fields_are_split_only_on_the_commas_between_them():
    assert split_header_fields('realm="a,b", nonce="c"') == ['realm="a,b"', 'nonce="c"']


def test_an_unquoted_value_still_splits():
    """Some firmware omits the quotes, and RFC 7616 makes algorithm a token."""
    assert parse_key_value_list("nonce=abc, algorithm=SHA-256") == {
        "nonce": "abc",
        "algorithm": "SHA-256",
    }


def test_a_trailing_comma_does_not_become_an_empty_field():
    """An empty field has no `=`, so it would raise and discard the whole
    challenge."""
    assert parse_key_value_list('nonce="abc", realm="r",') == {
        "nonce": "abc",
        "realm": "r",
    }


def test_a_value_containing_an_equals_sign_is_not_cut_at_it():
    """opaque and nonce are base64 often enough that the padding matters. The split
    is on the first `=` only, and this says so rather than leaving it to chance."""
    fields = parse_key_value_list('opaque="dGVzdA==", nonce="YWJjZA=="')

    assert fields == {"opaque": "dGVzdA==", "nonce": "YWJjZA=="}


def test_spaces_inside_a_quoted_value_are_kept():
    """`domain` and `realm` both carry them, and the realm goes into HA1 verbatim."""
    assert parse_key_value_list('realm="Login to device"')["realm"] == "Login to device"


def test_an_escaped_quote_does_not_end_the_quoted_value():
    r"""A realm containing \" would otherwise flip the parser out of the quoted
    string and make every later comma a separator again."""
    fields = split_header_fields(r'realm="a \" b, c", nonce="d"')

    assert fields == [r'realm="a \" b, c"', 'nonce="d"']


def test_a_lone_quote_is_not_read_as_an_empty_quoted_string():
    """Stripping the first and last character of a one-character value leaves
    nothing, and an empty nonce signs a digest against the wrong nonce rather than
    failing. The length guard is what stops that."""
    assert parse_pair('nonce="') == ("nonce", '"')


def test_a_field_with_no_name_is_no_challenge():
    """`Digest =` has to read as "nothing offered". It used to, via an IndexError
    off an empty value, which worked by accident."""
    with pytest.raises(ValueError):
        parse_pair("=value")

    auth = _auth()
    assert auth._parse_401(_Response(401, "Digest =")) is None


# --- 2. the algorithms a device may name ---------------------------------------


def test_sha_256_is_signed_with_sha_256():
    """RFC 7616's upgrade from MD5, and what firmware on a security baseline is
    liable to offer. This used to build no header at all."""
    auth = _for_challenge({"realm": REALM, "nonce": "n1", "algorithm": "SHA-256"})

    params = _params(auth._build_digest_header("GET", URL))

    ha1 = _sha256("%s:%s:%s" % (USER, REALM, PASSWORD))
    ha2 = _sha256("%s:%s" % ("GET", params["uri"]))
    assert params["response"] == _sha256("%s:%s:%s" % (ha1, "n1", ha2))
    assert params["response"] != _md5(
        "%s:%s:%s"
        % (
            _md5("%s:%s:%s" % (USER, REALM, PASSWORD)),
            "n1",
            _md5("%s:%s" % ("GET", params["uri"])),
        )
    ), "signed with MD5"


def test_the_algorithm_is_echoed_as_the_device_named_it():
    """RFC 7616 s3.4: the request carries the algorithm it was signed with, and a
    device that compares it will refuse a mismatch."""
    auth = _for_challenge({"realm": REALM, "nonce": "n1", "algorithm": "SHA-256"})

    assert _params(auth._build_digest_header("GET", URL))["algorithm"] == "SHA-256"


def test_sha_256_sess_folds_the_nonce_into_ha1():
    """The session variant is a suffix on any algorithm. MD5-SESS was handled by an
    equality test, so SHA-256-SESS was signed as plain SHA-256 -- a valid-looking
    header that authenticates as the wrong thing."""
    auth = _for_challenge(
        {"realm": REALM, "nonce": "n1", "qop": "auth", "algorithm": "SHA-256-SESS"}
    )

    params = _params(auth._build_digest_header("GET", URL))

    plain = _sha256("%s:%s:%s" % (USER, REALM, PASSWORD))
    session_ha1 = _sha256("%s:%s:%s" % (plain, "n1", params["cnonce"]))
    ha2 = _sha256("%s:%s" % ("GET", params["uri"]))
    noncebit = ":".join(["n1", params["nc"], params["cnonce"], "auth", ha2])

    assert params["response"] == _sha256("%s:%s" % (session_ha1, noncebit))
    assert params["response"] != _sha256(
        "%s:%s" % (plain, noncebit)
    ), "signed as plain SHA-256, so the session nonce was ignored"


@pytest.mark.parametrize("algorithm", ["sha-256", "SHA-256-sess", "md5-SESS"])
def test_the_algorithm_name_is_matched_whatever_its_case(algorithm):
    """RFC 7616 s3.3 makes it case insensitive, and firmware sends `-sess` lower
    while naming the digest upper."""
    auth = _for_challenge(
        {"realm": REALM, "nonce": "n1", "qop": "auth", "algorithm": algorithm}
    )

    assert auth._build_digest_header("GET", URL), algorithm


async def test_a_device_offering_sha_256_can_be_added():
    """End to end through request(), which is where the three dropped attempts and
    the final 401 came from."""
    device = _Device(
        'Digest realm="%s", qop="auth", nonce="abc", algorithm=SHA-256' % REALM
    )

    response = await _auth(device).request("GET", URL)

    assert response.status == 200


# --- 3. and saying so, for the ones that are genuinely unanswerable ------------


def test_an_algorithm_that_cannot_be_signed_says_which_one(caplog):
    """The absent credential is right. Being silent about it is what made #947
    undiagnosable, because the 401 it produces is reported as a wrong password."""
    auth = _for_challenge({"realm": REALM, "nonce": "n1", "algorithm": "SHA-512-256"})

    assert auth._build_digest_header("GET", URL) == ""

    assert "SHA-512-256" in _logged(caplog)
    assert _warnings(caplog)


async def test_a_scheme_this_does_not_implement_says_which_one(caplog):
    """Not every 401 is a digest or a Basic one, and no password answers an
    NTLM challenge."""
    device = _Device('Negotiate realm="x"')

    response = await _auth(device).request("GET", URL)

    assert response.status == 401
    assert "negotiate" in _logged(caplog).lower()
    assert _warnings(caplog)


def test_the_two_schemes_that_are_answerable_are_the_two_implemented():
    """A third name here without the code to match would make the warning lie."""
    assert ANSWERABLE_SCHEMES == {BASIC, DIGEST}


async def test_the_warning_does_not_repeat_for_the_same_device(caplog):
    """It does not clear itself: the same challenge comes back on every request, so
    warning each time would fill the log at the scan interval. The state is the one
    already shared across a host's requests."""
    state = {}
    for _ in range(4):
        await _auth(_Device("Negotiate"), state).request("GET", URL)

    warnings = _warnings(caplog)
    assert len(warnings) == 1, [r.getMessage() for r in warnings]


async def test_a_device_that_changes_its_mind_warns_again(caplog):
    """Firmware updates. A second scheme is a second thing worth saying."""
    state = {}
    await _auth(_Device("Negotiate"), state).request("GET", URL)
    await _auth(_Device("NTLM"), state).request("GET", URL)

    warnings = _warnings(caplog)
    assert len(warnings) == 2, [r.getMessage() for r in warnings]


async def test_an_ordinary_refused_password_does_not_warn(caplog):
    """The common case stays quiet. A wrong password is already reported to the
    user by the config flow, and a warning per poll for every one of them is how a
    useful warning stops being read."""
    caplog.set_level(logging.DEBUG)
    challenge = 'Digest realm="%s", qop="auth", nonce="abc"' % REALM

    class _AlwaysRefuses(_Device):
        async def request(self, method, url, headers=None, **kwargs):
            self.sent.append((headers or {}).get("AUTHORIZATION"))
            return _Response(401, self.challenge)

    response = await _auth(_AlwaysRefuses(challenge)).request("GET", URL)

    assert response.status == 401
    assert not _warnings(caplog)
    assert REALM in _logged(caplog), "nothing recorded what the device asked for"


async def test_what_is_logged_is_the_challenge_and_not_the_credentials(caplog):
    """The scheme, realm, qop and algorithm are the diagnostic payload and none is
    a secret. The password is not in the log, and neither is the URL, which is one
    endpoint away from carrying something that should not be."""
    caplog.set_level(logging.DEBUG)
    nonce = "a-nonce-no-other-log-line-could-carry"

    class _AlwaysRefuses(_Device):
        async def request(self, method, url, headers=None, **kwargs):
            return _Response(401, self.challenge)

    await _auth(
        _AlwaysRefuses(
            'Digest realm="%s", qop="auth", nonce="%s"' % (REALM, nonce),
        )
    ).request("GET", URL)

    logged = _logged(caplog)
    assert REALM in logged, "nothing was logged, so this proves nothing"
    assert PASSWORD not in logged
    assert URL not in logged
    assert nonce not in logged, "the nonce is noise, and it changes every request"
