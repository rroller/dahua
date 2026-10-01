"""Dahua Digest Auth Support"""
import base64
import logging
import os
import time
import hashlib
import aiohttp
from aiohttp.client_reqrep import ClientResponse
from aiohttp.client_exceptions import ClientError
from yarl import URL

_LOGGER: logging.Logger = logging.getLogger(__package__)


# Seems that aiohttp doesn't support Diegest Auth, which Dahua cams require. So I had to bake it in here.
# Copied and then modified from https://github.com/aio-libs/aiohttp/pull/2213
# I really wish this was baked into aiohttp :-(

# How many times one request may answer a 401 before giving up.
MAX_AUTH_ATTEMPTS = 3

# Firmware old enough to predate digest on the CGI interface answers with a
# Basic challenge instead, and #583 is what that looks like: the web UI works,
# `curl -u` works, `curl --digest -u` gets a 401 carrying `WWW-Authenticate:
# Basic realm="Device_CGI"`, and the integration reads that 401 as a wrong
# password and starts a reauth that cannot succeed. Reported on an
# IPC-HFW4300S-V2 on 2014 firmware and an IPC-HDW4300C on 2015 firmware.
#
# Only ever used when the device asks for it by name. Basic puts the password
# on the wire in a header, so it is not something to offer unprompted.
BASIC = "basic"
DIGEST = "digest"

# The two this module can answer. A 401 naming anything else cannot be satisfied by
# any password, so it must not be reported as a refused one.
ANSWERABLE_SCHEMES = frozenset({BASIC, DIGEST})

# RFC 7616 names the algorithm a device may pick, and allows a `-sess` variant of
# each. `algorithm=SHA-256` built no header at all here: _build_digest_header
# returned "", request() dropped the challenge and probed again, and the 401 three
# attempts later was reported to the user as a wrong username and password.
#
# SHA is not in the RFC. It is kept because it was already accepted, and some
# firmware does send it.
#
# SHA-512-256 is deliberately absent. RFC 7616 names it as NIST's truncated
# variant, which is not SHA-512 cut to 256 bits, and hashlib only reaches it
# through the OpenSSL name. Signing with a wrong SHA-512-256 would be refused
# anyway, and an absent credential that says so is more use than a silent one.
HASH_ALGORITHMS = {
    "MD5": hashlib.md5,
    "SHA": hashlib.sha1,
    "SHA-256": hashlib.sha256,
}

SESSION_SUFFIX = "-SESS"


class DigestAuth:
    """HTTP digest authentication helper.
    The work here is based off of
    https://github.com/requests/requests/blob/v2.18.4/requests/auth.py.
    """

    def __init__(self, username: str, password: str, session: aiohttp.ClientSession, previous=None):
        if previous is None:
            previous = {}

        self.username = username
        self.password = password
        # Held by reference, so a challenge accepted by one request is reused by
        # the next. Callers passing nothing keep the old per-request behaviour.
        self._state = previous
        self.session = session

    @property
    def scheme(self):
        """Which scheme this device asked for, once it has told us."""
        return self._state.get("scheme")

    @scheme.setter
    def scheme(self, value):
        self._state["scheme"] = value

    # Challenge and nonce count live in the shared state, exposed as attributes.
    @property
    def challenge(self):
        return self._state.get("challenge")

    @challenge.setter
    def challenge(self, value):
        self._state["challenge"] = value

    @property
    def last_nonce(self):
        return self._state.get("last_nonce", "")

    @last_nonce.setter
    def last_nonce(self, value):
        self._state["last_nonce"] = value

    @property
    def nonce_count(self):
        return self._state.get("nonce_count", 0)

    @nonce_count.setter
    def nonce_count(self, value):
        self._state["nonce_count"] = value

    async def request(self, method, url, *, headers=None, **kwargs):
        """Makes a request, absorbing digest challenges up to a fixed budget."""
        if headers is None:
            headers = {}

        refused = 0
        response = None

        for _ in range(MAX_AUTH_ATTEMPTS):
            attempt_headers = dict(headers)
            sent_nonce = None

            if self.scheme == BASIC:
                attempt_headers["AUTHORIZATION"] = self._build_basic_header()
            elif self.challenge:
                authorization = self._build_digest_header(method.upper(), url)
                if authorization:
                    attempt_headers["AUTHORIZATION"] = authorization
                    sent_nonce = self.challenge.get("nonce")
                else:
                    # A challenge we cannot build a header from would otherwise
                    # fail every later request too. Drop it and probe instead.
                    self.challenge = None

            response = await self.session.request(method, url, headers=attempt_headers, **kwargs)

            if response.status != 401:
                return response

            challenge = self._parse_401(response)
            if challenge is None:
                # A device that wants Basic says so here. Switch once and
                # retry; if it refuses that too, the credentials are wrong
                # and the 401 is the honest answer.
                if self._offered_scheme(response) == BASIC and self.scheme != BASIC:
                    self.scheme = BASIC
                    response.close()
                    continue
                return self._refused(response)

            if sent_nonce is not None:
                stale = str(challenge.get("stale", "")).lower() == "true"
                if challenge.get("nonce") == sent_nonce and not stale:
                    # Same nonce, not flagged stale: either the credentials are
                    # wrong or our nonce count arrived out of order. One retry
                    # covers the count; a second failure means it is the password.
                    refused += 1
                    if refused > 1:
                        self.challenge = None
                        return self._refused(response)

            response.close()
            self.challenge = challenge

        # The budget is spent. Every other exit returns before here, so this is
        # always a 401, and the loop always runs at least once.
        return self._refused(response)

    def _refused(self, response: ClientResponse):
        """Says what the device asked for, then hands the 401 back unchanged.

        Every 401 that reaches a caller is reported to the user as the camera
        rejecting their username and password. Nothing here used to record the
        challenge, so #947 arrived as a traceback ending in `401,
        message='Unauthorized'` and a model number, which does not distinguish a
        refused password from a challenge this module could not answer. Three of
        those existed: a comma inside a quoted value broke the parser, the SHA-256
        family signed nothing, and an unknown scheme is still genuinely unanswerable.

        The scheme, realm, qop and algorithm are what a reader needs and none of
        them is a secret. Nothing this module sent is logged, and neither is the
        URL, which is one endpoint away from carrying something that should not be.
        """
        offered = self._offered_scheme(response)

        if offered is not None and offered not in ANSWERABLE_SCHEMES:
            self._warn_once(
                "scheme", offered,
                "This device asked for %s authentication, which this integration "
                "does not implement. No password will work and the failure will look "
                "like a wrong username and password. WWW-Authenticate: %s",
                offered, response.headers.get("www-authenticate", ""))
            return response

        fields = self._offered_fields(response)
        _LOGGER.debug(
            "Credentials refused by a device offering %s auth (%s)",
            offered or "no", ", ".join(
                "%s=%s" % (name, fields[name])
                for name in ("realm", "qop", "algorithm", "stale")
                if fields.get(name)) or "no fields")
        return response

    def _warn_once(self, kind, value, message, *args):
        """Warns about something that cannot work, once per device.

        These conditions do not clear themselves: the same unanswerable challenge
        comes back on every request, so warning each time would fill the log at the
        scan interval. The state is the one already shared across this device's
        requests, and a different value warns again because firmware changes.
        """
        key = "unanswerable_" + kind
        if self._state.get(key) == value:
            return
        self._state[key] = value
        _LOGGER.warning(message, *args)

    @staticmethod
    def _offered_scheme(response: ClientResponse):
        """The auth scheme this 401 asked for, lowercased, or None."""
        header = response.headers.get("www-authenticate", "")
        if not header:
            return None
        return header.split(" ", 1)[0].lower() or None

    @staticmethod
    def _offered_fields(response: ClientResponse):
        """The challenge's parameters, whatever scheme named them. Credential-free."""
        parts = response.headers.get("www-authenticate", "").split(" ", 1)
        if len(parts) < 2:
            return {}
        try:
            return parse_key_value_list(parts[1])
        except (IndexError, ValueError):
            return {}

    def _build_basic_header(self):
        """RFC 7617: base64 of user:password, and nothing else."""
        raw = "{0}:{1}".format(self.username, self.password).encode("utf-8")
        return "Basic " + base64.b64encode(raw).decode("ascii")

    def _parse_401(self, response: ClientResponse):
        """Returns the digest challenge carried by a 401, or None."""
        if self._offered_scheme(response) != DIGEST:
            return None
        # A challenge naming no usable field is no challenge, same as one that
        # cannot be parsed at all.
        return self._offered_fields(response) or None

    def _build_digest_header(self, method, url):
        """
        :rtype: str
        """

        realm = self.challenge.get("realm")
        nonce = self.challenge.get("nonce")
        if realm is None or nonce is None:
            return ""
        qop = self.challenge.get("qop")
        algorithm = self.challenge.get("algorithm", "MD5").upper()
        opaque = self.challenge.get("opaque")

        if qop and not (qop == "auth" or "auth" in [part.strip() for part in qop.split(",")]):
            raise ClientError("Unsupported qop value: %s" % qop)

        # The session variant is a suffix on any of them, so it is tested as one
        # rather than enumerated: MD5-SESS was handled and SHA-256-SESS was not,
        # for no reason either algorithm knows about.
        session_variant = algorithm.endswith(SESSION_SUFFIX)
        base_algorithm = algorithm[: -len(SESSION_SUFFIX)] if session_variant else algorithm
        hash_fn = HASH_ALGORITHMS.get(base_algorithm)
        if hash_fn is None:
            self._warn_once(
                "algorithm", algorithm,
                "This device asked for digest algorithm %s, which this integration "
                "cannot sign. No credentials can be sent, so the device will answer "
                "401 and the failure will look like a wrong username and password. "
                "Please report the algorithm name on a new issue",
                algorithm)
            return ""

        def H(x):
            return hash_fn(x.encode()).hexdigest()

        def KD(s, d):
            return H("%s:%s" % (s, d))

        # raw_path_qs, not path_qs: the request-URI that goes on the wire is
        # percent-encoded by yarl, and RFC 7616 wants the uri in the header to
        # be that same string. path_qs hands back the decoded form, so every URL
        # carrying a square bracket -- which is every indexed write, from
        # MotionDetect[0].Enable to Lighting_V2[3][0][1] -- was signed as
        # "[0]" while "%5B0%5D" was sent. A device that checks is entitled to
        # refuse that, and 403 is what refusing it looks like.
        path = URL(url).raw_path_qs
        A1 = "%s:%s:%s" % (self.username, realm, self.password)
        A2 = "%s:%s" % (method, path)

        HA1 = H(A1)
        HA2 = H(A2)

        if nonce == self.last_nonce:
            self.nonce_count += 1
        else:
            self.nonce_count = 1

        self.last_nonce = nonce

        ncvalue = "%08x" % self.nonce_count

        # cnonce is just a random string generated by the client.
        cnonce_data = "".join(
            [
                str(self.nonce_count),
                nonce,
                time.ctime(),
                os.urandom(8).decode(errors="ignore"),
            ]
        ).encode()
        cnonce = hashlib.sha1(cnonce_data).hexdigest()[:16]

        if session_variant:
            HA1 = H("%s:%s:%s" % (HA1, nonce, cnonce))

        # This assumes qop was validated to be 'auth' above. If 'auth-int'
        # support is added this will need to change.
        if qop:
            noncebit = ":".join([nonce, ncvalue, cnonce, "auth", HA2])
            response_digest = KD(HA1, noncebit)
        else:
            response_digest = KD(HA1, "%s:%s" % (nonce, HA2))

        base = ", ".join(
            [
                'username="%s"' % self.username,
                'realm="%s"' % realm,
                'nonce="%s"' % nonce,
                'uri="%s"' % path,
                'response="%s"' % response_digest,
                'algorithm="%s"' % algorithm,
            ]
        )
        if opaque:
            base += ', opaque="%s"' % opaque
        if qop:
            base += ', qop="auth", nc=%s, cnonce="%s"' % (ncvalue, cnonce)

        return "Digest %s" % base


def parse_pair(pair):
    key, value = pair.strip().split("=", 1)
    key = key.strip()
    if not key:
        # A field with no name is malformed, and the callers read a ValueError as
        # "there is no challenge here". It used to arrive as an IndexError off
        # value[-1] below, which meant a bare `Digest =` was rejected by accident
        # rather than on purpose.
        raise ValueError("challenge field with no name: %r" % pair)

    value = value.strip()

    # A trailing comma, for a caller that split the header itself.
    # split_header_fields does not leave one.
    if value.endswith(","):
        value = value[:-1]

    # If it is quoted, then remove them. Guarded on length so a lone quote is not
    # read as an empty quoted string.
    if len(value) > 1 and value[0] == value[-1] == '"':
        value = value[1:-1]

    return key, value


def split_header_fields(header):
    """Splits a challenge on the commas that separate its fields, not on all of them.

    A comma inside a quoted value separates nothing, and two things devices really
    send rely on that:

        WWW-Authenticate: Digest realm="Login to 4KS2", qop="auth,auth-int", nonce="a"
        WWW-Authenticate: Digest realm="Login to device, channel 1", nonce="a"

    `qop="auth,auth-int"` is RFC 7616's ordinary way of offering both, and the realm
    is free text. Splitting on every comma turned either into fragments with no `=`
    in them, the ValueError read as "this 401 carries no digest challenge", and the
    request went back out unauthenticated until the attempt budget ran out. What the
    user is then told is that the camera rejected their username and password (#947),
    which is why #583 and this look identical from the outside and are not the same
    fault at all.
    """
    fields = []
    current = []
    quoted = False
    escaped = False

    for char in header:
        if escaped:
            # Part of a quoted-pair, so it cannot close the string whatever it is.
            current.append(char)
            escaped = False
        elif quoted and char == "\\":
            # RFC 7230 quoted-pair. The backslash is kept, so the value a device
            # without one sends is unchanged.
            current.append(char)
            escaped = True
        elif char == '"':
            quoted = not quoted
            current.append(char)
        elif char == "," and not quoted:
            fields.append("".join(current))
            current = []
        else:
            current.append(char)

    fields.append("".join(current))
    return [field for field in (candidate.strip() for candidate in fields) if field]


def parse_key_value_list(header):
    return dict(parse_pair(field) for field in split_header_fields(header))
