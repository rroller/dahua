"""Tests for custom_components.dahua.digest."""
import asyncio
import hashlib
import re

import pytest

from custom_components.dahua import client as client_module
from custom_components.dahua.client import DahuaClient
from aiohttp.client_exceptions import ClientError
from custom_components.dahua.digest import DigestAuth
from yarl import URL

REALM = "DahuaRpc"
USER = "admin"
PASSWORD = "secret"


@pytest.fixture(autouse=True)
def _clean_host_digest_state():
    """A challenge is now shared per host, so it outlives a test unless cleared."""
    client_module._HOST_DIGEST_STATE.clear()
    yield
    client_module._HOST_DIGEST_STATE.clear()


def _params(header: str) -> dict:
    """Parse the parameters out of an Authorization header."""
    body = header.partition(" ")[2]
    return {m[0]: (m[1] or m[2])
            for m in re.findall(r'(\w+)=(?:"([^"]*)"|([^,\s]+))', body)}


def _expected_response(method: str, params: dict, password: str) -> str:
    """Recompute the digest the client should have sent."""
    def h(value):
        return hashlib.md5(value.encode()).hexdigest()

    ha1 = h("%s:%s:%s" % (params.get("username", ""), REALM, password))
    ha2 = h("%s:%s" % (method, params.get("uri", "")))
    if params.get("qop"):
        return h(":".join([ha1, params.get("nonce", ""), params.get("nc", ""),
                           params.get("cnonce", ""), "auth", ha2]))
    return h("%s:%s:%s" % (ha1, params.get("nonce", ""), ha2))


class FakeResponse:
    def __init__(self, status, headers=None, body=""):
        self.status = status
        self.headers = headers or {}
        self.closed = False
        self._body = body

    def close(self):
        self.closed = True

    async def text(self):
        return self._body

    async def read(self):
        return self._body.encode()

    def raise_for_status(self):
        if self.status >= 400:
            raise AssertionError("unexpected %s in this test" % self.status)


class FakeSession:
    """A device that actually verifies the digest it is sent.

    Requests suspend before replying so concurrent callers genuinely overlap
    rather than each running to completion in one scheduler step.
    """

    def __init__(self, password=PASSWORD, nonce="nonce-1", strict_nc=False, body="ok=1",
                 strict_uri=False):
        self.requests = []
        self.password = password
        self.nonce = nonce
        self.strict_nc = strict_nc
        self.body = body
        self.seen_nc = set()
        # Some firmware checks that the uri in the header is the request-URI it
        # actually received, as RFC 7616 requires. aiohttp percent-encodes that
        # URI, so the fake has to do the same before judging what it was sent.
        self.strict_uri = strict_uri

    def _challenge(self, stale=False):
        header = 'Digest realm="%s", nonce="%s", qop="auth"' % (REALM, self.nonce)
        if stale:
            header += ', stale="true"'
        return {"www-authenticate": header}

    async def request(self, method, url, headers=None, **kwargs):
        headers = headers or {}
        self.requests.append({"method": method, "url": url, "headers": dict(headers)})
        await asyncio.sleep(0)  # let siblings interleave

        auth = headers.get("AUTHORIZATION")
        if not auth:
            return FakeResponse(401, self._challenge())

        params = _params(auth)
        if params.get("nonce") != self.nonce:
            return FakeResponse(401, self._challenge(stale=True))
        if params.get("response") != _expected_response(method.upper(), params, self.password):
            return FakeResponse(401, self._challenge())
        if self.strict_uri and params.get("uri") != URL(url).raw_path_qs:
            # Credentials were fine; the signature does not cover this request.
            return FakeResponse(403, body="Forbidden")
        if self.strict_nc:
            nc = params.get("nc")
            if nc in self.seen_nc:
                # RFC 7616 5.5: a replayed count is refused with the same nonce.
                return FakeResponse(401, self._challenge())
            self.seen_nc.add(nc)

        return FakeResponse(200, body=self.body)


def nc_of(request):
    match = re.search(r"nc=([0-9a-f]{8})", request["headers"].get("AUTHORIZATION", ""))
    return match.group(1) if match else None


async def test_challenge_is_reused_across_requests():
    """The first call absorbs a 401; later calls authenticate up front."""
    session = FakeSession()
    state = {}

    first = await DigestAuth(USER, PASSWORD, session, state).request("GET", "http://d/one")
    assert first.status == 200
    assert len(session.requests) == 2  # probe, then authenticated retry

    second = await DigestAuth(USER, PASSWORD, session, state).request("GET", "http://d/two")
    assert second.status == 200
    assert len(session.requests) == 3  # no second probe
    assert "AUTHORIZATION" in session.requests[2]["headers"]


async def test_client_reuses_one_challenge_across_calls():
    """The client must thread one challenge through every call site."""
    session = FakeSession(body="deviceType=NVR")
    client = DahuaClient(USER, PASSWORD, "d", 80, 554, session)

    await client.get("/cgi-bin/magicBox.cgi?action=getSystemInfo")
    await client.get("/cgi-bin/magicBox.cgi?action=getDeviceType")

    # Four requests would mean each call re-challenged independently.
    assert len(session.requests) == 3


async def test_without_shared_state_every_request_rechallenges():
    """Callers that pass no state keep the old behaviour."""
    session = FakeSession()

    for _ in range(3):
        await DigestAuth(USER, PASSWORD, session).request("GET", "http://d/x")

    assert len(session.requests) == 6  # two per call


async def test_nonce_count_increments_across_requests():
    """nc must advance per RFC 2617 while the nonce is unchanged."""
    session = FakeSession()
    state = {}

    for _ in range(3):
        await DigestAuth(USER, PASSWORD, session, state).request("GET", "http://d/x")

    assert [nc_of(r) for r in session.requests if nc_of(r)] == [
        "00000001", "00000002", "00000003"]


async def test_concurrent_requests_get_distinct_nonce_counts():
    """Requests genuinely in flight together must not reuse a count."""
    session = FakeSession()
    state = {}

    await DigestAuth(USER, PASSWORD, session, state).request("GET", "http://d/prime")
    before = len(session.requests)

    results = await asyncio.gather(*[
        DigestAuth(USER, PASSWORD, session, state).request("GET", "http://d/%d" % i)
        for i in range(5)
    ])

    assert [r.status for r in results] == [200] * 5
    counts = [nc_of(r) for r in session.requests[before:]]
    assert len(set(counts)) == len(counts) == 5, "nonce counts collided: %s" % counts


async def test_out_of_order_nonce_count_recovers():
    """A device refusing a replayed count must not fail the call outright."""
    session = FakeSession(strict_nc=True)
    state = {}
    await DigestAuth(USER, PASSWORD, session, state).request("GET", "http://d/prime")

    # Replay the count the priming call already burned.
    state["last_nonce"] = ""
    state["nonce_count"] = 0

    response = await DigestAuth(USER, PASSWORD, session, state).request("GET", "http://d/x")
    assert response.status == 200


async def test_rejected_credentials_stop_after_budget():
    """A 401 that keeps its challenge header must not retry forever."""
    session = FakeSession(password="something-else")

    response = await DigestAuth(USER, "wrong", session, {}).request("GET", "http://d/x")

    assert response.status == 401
    assert len(session.requests) <= 3


async def test_stale_challenge_is_refreshed_and_succeeds():
    """A rotated nonce is re-learned and the call still succeeds."""
    session = FakeSession(nonce="nonce-1")
    state = {}
    await DigestAuth(USER, PASSWORD, session, state).request("GET", "http://d/x")
    assert state["challenge"]["nonce"] == "nonce-1"

    session.nonce = "nonce-2"
    before = len(session.requests)

    response = await DigestAuth(USER, PASSWORD, session, state).request("GET", "http://d/y")
    assert response.status == 200
    assert state["challenge"]["nonce"] == "nonce-2"
    assert len(session.requests) - before == 2  # stale attempt, then success


async def test_unusable_cached_challenge_is_discarded():
    """A cached challenge missing required fields must not poison later calls."""
    session = FakeSession()
    state = {"challenge": {"qop": "auth"}}  # no realm, no nonce

    response = await DigestAuth(USER, PASSWORD, session, state).request("GET", "http://d/x")

    assert response.status == 200
    assert state["challenge"]["nonce"] == session.nonce


# --- the uri the header signs -------------------------------------------------

INDEXED_WRITE = ("http://d/cgi-bin/configManager.cgi?action=setConfig"
                 "&Lighting_V2[3][0][1].Mode=Manual&Lighting_V2[3][0][1].NearLight[0].Light=100")


async def test_the_header_signs_the_uri_that_is_actually_sent():
    """Every indexed write carries square brackets, and aiohttp encodes them.

    The header used to be built from the decoded path, so it said "[3]" while
    "%5B3%5D" went on the wire. A device that checks is entitled to refuse.
    """
    session = FakeSession(strict_uri=True)

    response = await DigestAuth(USER, PASSWORD, session, {}).request("GET", INDEXED_WRITE)

    assert response.status == 200, "the device refused a signature it could not verify"


async def test_the_signed_uri_is_the_encoded_form():
    session = FakeSession()

    await DigestAuth(USER, PASSWORD, session, {}).request("GET", INDEXED_WRITE)

    uri = _params(session.requests[-1]["headers"]["AUTHORIZATION"])["uri"]
    assert "%5B3%5D" in uri
    assert "[3]" not in uri, "the header still names the decoded path"


async def test_a_url_without_brackets_is_unaffected():
    """Nothing changes for the URLs that have no character needing encoding."""
    session = FakeSession(strict_uri=True)
    plain = "http://d/cgi-bin/magicBox.cgi?action=getMachineName"

    response = await DigestAuth(USER, PASSWORD, session, {}).request("GET", plain)

    assert response.status == 200
    uri = _params(session.requests[-1]["headers"]["AUTHORIZATION"])["uri"]
    assert uri == "/cgi-bin/magicBox.cgi?action=getMachineName"



# --- the RFC 7616 variations different firmware actually offers ----------------
#
# Everything above drives the whole exchange against a device offering MD5 with
# qop="auth", which is what the cameras here send. The branches for the other shapes had
# no tests, and they are not hypothetical: which algorithm and whether qop is offered at
# all is up to the device, and a camera that offers something unexpected fails
# authentication with nothing to say why.
#
# These call _build_digest_header directly, because going through a session would need a
# fake device that verifies each variant, which is a second implementation of the thing
# under test.

DIGEST_URL = "/cgi-bin/magicBox.cgi?action=getSystemInfo"


def _auth(challenge):
    auth = DigestAuth(USER, PASSWORD, FakeSession())
    auth.challenge = dict(challenge)
    return auth


def _md5(value):
    return hashlib.md5(value.encode()).hexdigest()


def _sha1(value):
    return hashlib.sha1(value.encode()).hexdigest()


def test_a_device_offering_sha_is_signed_with_sha1():
    """`algorithm="SHA"` is legal and some firmware sends it. Signing it with MD5 would
    be refused with a 401 that looks like a wrong password."""
    auth = _auth({"realm": REALM, "nonce": "n1", "algorithm": "SHA"})

    header = auth._build_digest_header("GET", DIGEST_URL)
    params = _params(header)

    ha1 = _sha1("%s:%s:%s" % (USER, REALM, PASSWORD))
    ha2 = _sha1("%s:%s" % ("GET", params["uri"]))
    assert params["response"] == _sha1("%s:%s:%s" % (ha1, "n1", ha2))


def test_md5_sess_folds_the_nonce_into_ha1():
    """MD5-SESS re-hashes HA1 with the nonce and the client nonce, so the same password
    produces a different HA1 per session. Treating it as plain MD5 authenticates as the
    wrong thing."""
    # With qop, because the client nonce only reaches the header when qop is offered
    # and the header is the only place a test can read it from.
    auth = _auth({"realm": REALM, "nonce": "n1", "qop": "auth",
                  "algorithm": "MD5-SESS"})

    header = auth._build_digest_header("GET", DIGEST_URL)
    params = _params(header)

    plain = _md5("%s:%s:%s" % (USER, REALM, PASSWORD))
    session_ha1 = _md5("%s:%s:%s" % (plain, "n1", params["cnonce"]))
    ha2 = _md5("%s:%s" % ("GET", params["uri"]))
    expected = _md5(":".join([session_ha1, "n1", params["nc"], params["cnonce"],
                              "auth", ha2]))
    as_plain_md5 = _md5(":".join([plain, "n1", params["nc"], params["cnonce"],
                                  "auth", ha2]))

    assert params["response"] == expected
    assert params["response"] != as_plain_md5, (
        "signed as plain MD5, so the session nonce was ignored")


def test_a_device_that_offers_no_qop_is_signed_the_older_way():
    """Without qop there is no nonce count and no client nonce in the response, and
    sending them anyway is a different digest. RFC 2069 rather than 7616, which older
    firmware still speaks."""
    auth = _auth({"realm": REALM, "nonce": "n1"})

    header = auth._build_digest_header("GET", DIGEST_URL)
    params = _params(header)

    ha1 = _md5("%s:%s:%s" % (USER, REALM, PASSWORD))
    ha2 = _md5("%s:%s" % ("GET", params["uri"]))
    assert params["response"] == _md5("%s:%s:%s" % (ha1, "n1", ha2))
    assert "qop" not in params
    assert "nc" not in params


def test_an_algorithm_nobody_here_implements_signs_nothing():
    """Returning an empty header rather than a wrong one. A guessed signature would be
    refused anyway, and this way the failure is the absent credential it really is."""
    auth = _auth({"realm": REALM, "nonce": "n1", "algorithm": "SHA-512-256"})

    assert auth._build_digest_header("GET", DIGEST_URL) == ""


def test_a_qop_this_cannot_do_is_refused_rather_than_faked():
    """auth-int signs the body as well, which is not implemented. Sending an `auth`
    digest and claiming auth-int would be wrong on the wire."""
    auth = _auth({"realm": REALM, "nonce": "n1", "qop": "auth-int"})

    with pytest.raises(ClientError):
        auth._build_digest_header("GET", DIGEST_URL)


def test_a_device_offering_both_qops_is_accepted():
    """A comma separated list including auth is usable, and is what the check is for."""
    auth = _auth({"realm": REALM, "nonce": "n1", "qop": "auth,auth-int"})

    assert _params(auth._build_digest_header("GET", DIGEST_URL))["qop"] == "auth"


# --- parsing what the device sent back ----------------------------------------

def test_a_challenge_that_cannot_be_parsed_is_no_challenge():
    """A malformed www-authenticate has to read as "no digest offered" rather than
    raising inside the request, which would turn a confused device into a traceback."""
    auth = DigestAuth(USER, PASSWORD, FakeSession())

    assert auth._parse_401(FakeResponse(401, {"www-authenticate": "Digest ="})) is None


def test_a_header_offering_no_digest_is_no_challenge():
    auth = DigestAuth(USER, PASSWORD, FakeSession())

    assert auth._parse_401(FakeResponse(401, {"www-authenticate": "Basic"})) is None
    assert auth._parse_401(FakeResponse(401, {})) is None


def test_a_trailing_comma_is_not_part_of_the_value():
    """Dahua devices send the challenge as a comma separated list, and the split leaves
    the comma on the value of every field but the last. A nonce with a comma stuck to it
    signs a different digest."""
    from custom_components.dahua.digest import parse_pair

    assert parse_pair("nonce=abc123,") == ("nonce", "abc123")
    assert parse_pair('realm="DahuaRpc",') == ("realm", "DahuaRpc")
    assert parse_pair('realm="DahuaRpc"') == ("realm", "DahuaRpc")
