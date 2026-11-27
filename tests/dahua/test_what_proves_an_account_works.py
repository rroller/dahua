"""What counts as the device accepting an account (#1241, second review).

A paused event stream resumes only when the device has accepted the
credentials it attaches with. A poll finishing is not that: rroller ran a
coordinator that finished a refresh having sent the camera nothing, and the
stream attached again on it. Nor is an answer from the read cache, which is
keyed by user and not by password.

So the evidence is taken in the digest exchange: a 2xx to a request that
carried this username and this password. These run the real `DigestAuth` and
the real client against the suite's fake device, which verifies the digest it
is sent.

**The third review** found the device that leaves out: one that answers only
over RPC2 never completes a digest exchange, so a stream started while the
address's budget was spent waited for ever, with its owner reading the device
successfully throughout. The second half of this file is RPC2: a login is the
device accepting the password it was made with, and an answer on that session
is the device still honouring it, until those same credentials are refused.
"""

import asyncio

import aiohttp
import pytest

from custom_components.dahua import client as client_module
from custom_components.dahua.client import (
    DahuaClient,
    credentials_acceptance_mark,
    credentials_accepted_since,
)
from custom_components.dahua.digest import DigestAuth
from custom_components.dahua.host import async_record_host_success

from .test_digest import PASSWORD, USER, FakeResponse, FakeSession
from .test_refused_credentials import (
    ADDRESS,
    _no_issue_registry,
    _spend_the_budget,
    _stream,
    no_wait,
)

URL = "/cgi-bin/magicBox.cgi?action=getSystemInfo"


@pytest.fixture(autouse=True)
def _clean():
    client_module._HOST_DIGEST_STATE.clear()
    client_module._CREDENTIALS_ACCEPTED.clear()
    client_module._CREDENTIALS_REFUSED.clear()
    yield
    client_module._HOST_DIGEST_STATE.clear()
    client_module._CREDENTIALS_ACCEPTED.clear()
    client_module._CREDENTIALS_REFUSED.clear()


def _client(session, password=PASSWORD, port=80):
    return DahuaClient(USER, password, "d", port, 554, session)


# --- the exchange -----------------------------------------------------------------


async def _exchange(session, password=PASSWORD, **kwargs):
    heard = []
    auth = DigestAuth(USER, password, session, {})
    auth.on_accepted = lambda: heard.append("accepted")
    response = await auth.request("GET", "http://d/x", **kwargs)
    return response.status, heard


async def test_a_2xx_to_a_signed_request_is_the_device_accepting_the_account():
    status, heard = await _exchange(FakeSession())

    assert status == 200
    assert heard == ["accepted"]


async def test_a_refused_password_is_not():
    status, heard = await _exchange(FakeSession(), password="wrong")

    assert status == 401
    assert heard == []


async def test_a_2xx_to_a_request_that_carried_no_credentials_is_not():
    """A device, or a path, that answers without asking has not been shown a
    password at all."""

    class _AsksNothing:
        def __init__(self):
            self.requests = []

        async def request(self, method, url, headers=None, **kwargs):
            self.requests.append(dict(headers or {}))
            return FakeResponse(200, body="ok=1")

    session = _AsksNothing()
    status, heard = await _exchange(session)

    assert status == 200
    assert "AUTHORIZATION" not in session.requests[0]
    assert heard == []


async def test_a_signed_request_the_device_answered_with_an_error_is_not():
    """The fake checks the signature covers the URI and answers 403 when it
    does not. The password was right; this still is not a 2xx."""
    session = FakeSession(strict_uri=True)
    auth = DigestAuth(USER, PASSWORD, session, {})
    heard = []
    auth.on_accepted = lambda: heard.append("accepted")
    auth._build_digest_header = _signing_another_uri(auth._build_digest_header)

    response = await auth.request("GET", "http://d/x")

    assert response.status == 403
    assert heard == []


def _signing_another_uri(build):
    return lambda method, url: build(method, "http://d/y")


async def test_nobody_listening_is_not_an_error():
    response = await DigestAuth(USER, PASSWORD, FakeSession(), {}).request(
        "GET", "http://d/x"
    )

    assert response.status == 200


# --- through the client -------------------------------------------------------------


async def test_a_read_the_device_answered_is_evidence_for_that_client():
    client = _client(FakeSession(body="deviceType=NVR"))
    mark = credentials_acceptance_mark()

    await client.get(URL)

    assert credentials_accepted_since(client, mark)


async def test_a_read_the_device_refused_is_not():
    session = FakeSession()
    client = _client(session, password="wrong")
    mark = credentials_acceptance_mark()

    # The suite's fake response raises AssertionError for any 4xx, where
    # aiohttp raises ClientResponseError. Either way the read failed.
    with pytest.raises((aiohttp.ClientResponseError, AssertionError)):
        await client.get(URL)
    assert session.requests[-1]["headers"].get(
        "AUTHORIZATION"
    ), "the wrong password was offered, and refused"

    assert not credentials_accepted_since(client, mark)


async def test_an_answer_from_the_cache_is_not_evidence_for_another_password():
    """Two entries, one device, one username, two passwords: the old one in an
    entry not yet updated. The cache is keyed by device, user and URL, so the
    second entry is handed the first one's answer and sends nothing."""
    session = FakeSession(body="deviceType=NVR")
    current = _client(session)
    outdated = _client(session, password="old")
    mark = credentials_acceptance_mark()

    await current.get(URL)
    sent = len(session.requests)
    answer = await outdated.get(URL)

    assert answer, "it was answered"
    assert len(session.requests) == sent, "from the cache: nothing was sent"
    assert credentials_accepted_since(current, mark)
    assert not credentials_accepted_since(outdated, mark)


async def test_an_answer_from_the_cache_is_not_new_evidence_for_the_same_client():
    """What a poll with nothing due looks like from here."""
    client = _client(FakeSession(body="deviceType=NVR"))
    await client.get(URL)
    mark = credentials_acceptance_mark()

    await client.get(URL)

    assert not credentials_accepted_since(client, mark)


async def test_a_still_is_evidence_too():
    """`get_bytes` signs its own request, outside the read cache."""
    client = _client(FakeSession(body="jpeg"))
    mark = credentials_acceptance_mark()

    await client.get_bytes("/cgi-bin/snapshot.cgi?channel=1")

    assert credentials_accepted_since(client, mark)


async def test_another_port_on_the_address_is_another_device():
    session = FakeSession(body="deviceType=NVR")
    one = _client(session)
    other = _client(session, port=81)
    mark = credentials_acceptance_mark()

    await one.get(URL)

    assert credentials_accepted_since(one, mark)
    assert not credentials_accepted_since(other, mark)


# --- a device that answers only over RPC2 ------------------------------------------
#
# Nothing here completes a digest exchange. The session is the real
# `_SharedRpc2Session`, reached through the real `_shared_rpc2` and
# `_rpc2_shared_call`; only the RPC2 client behind it is a double.


class _Rpc2Device:
    """An RPC2 endpoint that lets in one password and counts its logins."""

    def __init__(self, username, password, accepts=PASSWORD):
        self._username = username
        self._password = password
        self.accepts = accepts
        self.logins = 0
        self.asked = []

    async def login(self):
        self.logins += 1
        if self._password != self.accepts:
            raise ConnectionError("Dahua RPC2 login refused")
        return {"result": True, "params": {"keepAliveInterval": 60}}

    async def get_config(self, params):
        self.asked.append(params["name"])
        return {"table": [{"Enable": True}]}

    async def logout(self):
        return None


class _Idle:
    closed = False

    async def close(self):
        self.closed = True


@pytest.fixture
async def rpc2_only():
    """Builds clients for a device with no usable CGI, and tidies up after."""
    made = []

    def _make(password=PASSWORD, port=80, accepts=PASSWORD):
        client = DahuaClient(USER, password, ADDRESS, port, 554, None)
        key = client._rpc2_key()
        if key not in client_module._HOST_RPC2:
            client_module._HOST_RPC2[key] = client_module._SharedRpc2Session(
                _Idle(), _Rpc2Device(USER, password, accepts), None
            )
        made.append(key)
        return client

    yield _make
    for key in made:
        holder = client_module._HOST_RPC2.pop(key, None)
        if holder is not None and holder.keepalive is not None:
            holder.keepalive.cancel()
            with pytest.raises(asyncio.CancelledError):
                await holder.keepalive


def _read(client):
    return client._rpc2_shared_call(
        lambda rpc2: rpc2.get_config({"name": "MotionDetect"})
    )


async def test_an_rpc2_login_is_the_device_accepting_that_password(rpc2_only):
    client = rpc2_only()
    mark = credentials_acceptance_mark()

    await _read(client)

    assert credentials_accepted_since(client, mark)


async def test_an_rpc2_login_the_device_refused_is_not(rpc2_only):
    client = rpc2_only(password="wrong")
    mark = credentials_acceptance_mark()

    with pytest.raises(ConnectionError):
        await _read(client)

    assert not credentials_accepted_since(client, mark)


async def test_an_answer_on_a_session_already_open_is_evidence_too(rpc2_only):
    """The stranded case. The session was opened before the stream began to
    wait, and a device that holds one open need never be logged in to again."""
    client = rpc2_only()
    await _read(client)
    mark = credentials_acceptance_mark()

    await _read(client)

    assert credentials_accepted_since(client, mark)
    assert client_module._HOST_RPC2[client._rpc2_key()].client.logins == 1


async def test_a_session_opened_with_another_password_proves_nothing_about_this_one(
    rpc2_only,
):
    """The session is shared by device and username, and it was opened with
    the first entry's password. An entry still holding the old one rides on it
    and is answered, having shown the device nothing."""
    current = rpc2_only()
    outdated = rpc2_only(password="old")
    mark = credentials_acceptance_mark()

    await _read(current)
    assert await _read(outdated)

    assert credentials_accepted_since(current, mark)
    assert not credentials_accepted_since(outdated, mark)


async def test_a_session_stops_being_evidence_once_its_credentials_are_refused(
    rpc2_only,
):
    """A session can outlive the password that opened it. Once the device has
    refused these credentials on a signed request, an answer on the session
    they opened earlier is the session being honoured and no more than that."""
    client = rpc2_only()
    await _read(client)
    client_module.note_credentials_refused(client)
    mark = credentials_acceptance_mark()

    await _read(client)

    assert not credentials_accepted_since(client, mark)


async def test_a_login_after_the_refusal_is_evidence_again(rpc2_only):
    client = rpc2_only()
    await _read(client)
    client_module.note_credentials_refused(client)
    holder = client_module._HOST_RPC2[client._rpc2_key()]
    holder.task = None
    mark = credentials_acceptance_mark()

    await _read(client)

    assert holder.client.logins == 2
    assert credentials_accepted_since(client, mark)


async def test_a_refusal_of_another_password_does_not_void_this_session(rpc2_only):
    client = rpc2_only()
    await _read(client)
    client_module.note_credentials_refused(
        DahuaClient(USER, "old", ADDRESS, 80, 554, None)
    )
    mark = credentials_acceptance_mark()

    await _read(client)

    assert credentials_accepted_since(client, mark)


async def test_a_refused_digest_exchange_is_recorded_against_those_credentials():
    """Where the refusal above comes from in the field: `_refused` is the one
    way a 401 leaves the exchange."""
    client = _client(FakeSession(), password="wrong")
    before = credentials_acceptance_mark()

    with pytest.raises((aiohttp.ClientResponseError, AssertionError)):
        await client.get(URL)

    assert (
        client_module._CREDENTIALS_REFUSED[client_module.credentials_identity(client)]
        > before
    )
    assert _client(FakeSession()) is not None
    assert (
        client_module.credentials_identity(_client(FakeSession()))
        not in client_module._CREDENTIALS_REFUSED
    )


# --- and the stream that was left waiting ---------------------------------------------


async def _wait_then(stream, act):
    """Run the stream's real wait, do `act` while it waits, and see it through."""
    task = asyncio.ensure_future(stream._async_run())
    for _ in range(5):
        await asyncio.sleep(0)
    waited_first = list(stream.attaches) == []
    await act()
    with pytest.raises(asyncio.CancelledError):
        await task
    return waited_first


async def test_a_stream_whose_owner_answers_only_over_rpc2_attaches(no_wait, rpc2_only):
    """The review's device. The address's budget is spent when the stream
    starts, its owner then reads the device over RPC2 and the poll clears the
    budget. Before this the stream looked a hundred times and never attached."""
    client = rpc2_only()
    await _read(client)
    _spend_the_budget()
    stream = _stream(lambda n: None, client=client, refuse_first_attach=False)

    async def _owner_polls():
        await _read(client)
        async_record_host_success(None, ADDRESS)

    assert await _wait_then(stream, _owner_polls)

    assert len(stream.attaches) == 1
    assert len(stream.checks) < 100, "it was cancelled waiting, not let in"


async def test_an_rpc2_owner_holding_an_old_password_still_waits(no_wait, rpc2_only):
    """#729 kept whole on this transport: the other entry's session answers
    this one's reads, and that is not this entry's account being let in."""
    current = rpc2_only()
    outdated = rpc2_only(password="old")
    await _read(current)
    _spend_the_budget()
    stream = _stream(lambda n: None, client=outdated, refuse_first_attach=False)

    async def _both_poll():
        await _read(current)
        await _read(outdated)
        async_record_host_success(None, ADDRESS)

    assert await _wait_then(stream, _both_poll)

    assert stream.attaches == []
    assert len(stream.checks) == 100


# --- a 401 that was shown no password (fourth review) -----------------------------------
#
# rroller opened a working RPC2 session, got a bare 401 on an unsigned CGI
# read, and the next successful RPC2 read on that session no longer counted.
# The CGI endpoint never saw the password, so it refused nothing.


class _Bare401:
    """An endpoint that answers 401 with no challenge, whatever it is sent."""

    def __init__(self):
        self.requests = []

    async def request(self, method, url, headers=None, **kwargs):
        self.requests.append(dict(headers or {}))
        return FakeResponse(401)


async def _heard(session, password=PASSWORD, state=None, **kwargs):
    heard = []
    auth = DigestAuth(USER, password, session, {} if state is None else state)
    auth.on_refused = lambda: heard.append("refused")
    response = await auth.request("GET", "http://d/x", **kwargs)
    return response.status, heard


async def test_a_401_to_a_request_that_carried_no_credentials_refuses_nothing():
    session = _Bare401()

    status, heard = await _heard(session)

    assert status == 401
    assert all("AUTHORIZATION" not in sent for sent in session.requests)
    assert heard == []


async def test_a_401_to_a_request_that_carried_them_is_a_refusal():
    """The same endpoint, asked by a client that already holds a challenge
    and so signs its first attempt."""
    state = {}
    await DigestAuth(USER, PASSWORD, FakeSession(), state).request("GET", "http://d/x")
    session = _Bare401()

    status, heard = await _heard(session, state=state)

    assert status == 401
    assert "AUTHORIZATION" in session.requests[0]
    assert heard == ["refused"]


async def test_a_wrong_password_is_still_a_refusal():
    status, heard = await _heard(FakeSession(), password="wrong")

    assert status == 401
    assert heard == ["refused"]


async def test_an_unsigned_401_on_cgi_does_not_cancel_a_working_rpc2_session(
    rpc2_only,
):
    """The review's sequence, through the real client on both transports."""
    client = rpc2_only()
    client._session = _Bare401()
    await _read(client)

    with pytest.raises((aiohttp.ClientResponseError, AssertionError)):
        await client.get(URL)
    assert all("AUTHORIZATION" not in sent for sent in client._session.requests)
    mark = credentials_acceptance_mark()

    await _read(client)

    assert credentials_accepted_since(client, mark)
    assert client_module._CREDENTIALS_REFUSED == {}


# --- an acceptance the device has since taken back (fifth review) -----------------------
#
# rroller ran the stream loop: an accepted request cleared the host's budget,
# a signed 401 for the same account followed, and the paused stream then
# looked. The acceptance was newer than the start of its wait, so it attached
# again, after the newer refusal.


async def test_an_acceptance_followed_by_a_refusal_is_not_an_acceptance():
    client = _client(FakeSession())
    mark = credentials_acceptance_mark()

    client_module.note_credentials_accepted(client)
    assert credentials_accepted_since(client, mark)
    client_module.note_credentials_refused(client)

    assert not credentials_accepted_since(client, mark)


async def test_an_acceptance_after_the_refusal_counts_again():
    client = _client(FakeSession())
    mark = credentials_acceptance_mark()

    client_module.note_credentials_accepted(client)
    client_module.note_credentials_refused(client)
    client_module.note_credentials_accepted(client)

    assert credentials_accepted_since(client, mark)


async def test_a_refusal_before_the_acceptance_does_not_undo_it():
    client = _client(FakeSession())
    client_module.note_credentials_refused(client)
    mark = credentials_acceptance_mark()

    client_module.note_credentials_accepted(client)

    assert credentials_accepted_since(client, mark)


async def test_another_accounts_refusal_does_not_undo_this_ones_acceptance():
    """Two entries on one device with two passwords: the outdated one being
    refused says nothing about the current one."""
    current = _client(FakeSession())
    outdated = _client(FakeSession(), password="old")
    mark = credentials_acceptance_mark()

    client_module.note_credentials_accepted(current)
    client_module.note_credentials_refused(outdated)

    assert credentials_accepted_since(current, mark)


def _on_the_stream_address():
    return DahuaClient(USER, PASSWORD, ADDRESS, 80, 554, None)


async def test_a_stream_does_not_attach_on_an_acceptance_already_taken_back(no_wait):
    """The review's ordering, through the real wait loop: accepted, then
    refused, then the stream looks. It used to attach on that look."""
    client = _on_the_stream_address()
    _spend_the_budget()

    def _between_looks(n):
        if n == 3:
            client_module.note_credentials_accepted(client)
            async_record_host_success(None, ADDRESS)
            client_module.note_credentials_refused(client)

    stream = _stream(_between_looks, client=client, refuse_first_attach=False)

    with pytest.raises(asyncio.CancelledError):
        await stream._async_run()

    assert stream.attaches == [], "it attached after the newer refusal"
    assert len(stream.checks) == 100


async def test_and_attaches_once_the_account_is_accepted_after_that(no_wait):
    client = _on_the_stream_address()
    _spend_the_budget()

    def _between_looks(n):
        if n == 3:
            client_module.note_credentials_accepted(client)
            async_record_host_success(None, ADDRESS)
            client_module.note_credentials_refused(client)
        if n == 6:
            client_module.note_credentials_accepted(client)

    stream = _stream(_between_looks, client=client, refuse_first_attach=False)

    with pytest.raises(asyncio.CancelledError):
        await stream._async_run()

    assert len(stream.attaches) == 1
    assert len(stream.checks) == 6


# --- a refused RPC2 login is a refusal too ------------------------------------------------
#
# The same hole as the fifth review's, on the other transport: accepted over
# CGI, then turned down at RPC2 login, then the stream looks.


class _RefusesLogin(_Rpc2Device):
    def __init__(self, error):
        super().__init__(USER, PASSWORD)
        self.error = error

    async def login(self):
        self.logins += 1
        raise self.error


def _session_that_refuses(client, error):
    key = client._rpc2_key()
    client_module._HOST_RPC2[key] = client_module._SharedRpc2Session(
        _Idle(), _RefusesLogin(error), None
    )
    return key


@pytest.mark.parametrize(
    "error",
    [
        client_module.Rpc2MethodRefused(
            "login refused", code=268632085, method=client_module.RPC2_LOGIN_METHOD
        ),
        client_module.DhipLoginRefused("login refused"),
    ],
)
async def test_a_login_the_device_turns_down_undoes_an_earlier_acceptance(error):
    client = DahuaClient(USER, PASSWORD, ADDRESS, 80, 554, None)
    key = _session_that_refuses(client, error)
    mark = credentials_acceptance_mark()
    client_module.note_credentials_accepted(client)
    assert credentials_accepted_since(client, mark)

    try:
        with pytest.raises(Exception):
            await _read(client)
    finally:
        client_module._HOST_RPC2.pop(key, None)

    assert not credentials_accepted_since(client, mark)


@pytest.mark.parametrize(
    "error",
    [
        ConnectionError("dropped"),
        TimeoutError(),
        # The device declining something that is not the login.
        client_module.Rpc2MethodRefused("no", method="configManager.getConfig"),
    ],
)
async def test_a_login_that_merely_failed_undoes_nothing(error):
    """A dropped connection has shown the device no password."""
    client = DahuaClient(USER, PASSWORD, ADDRESS, 80, 554, None)
    key = _session_that_refuses(client, error)
    mark = credentials_acceptance_mark()
    client_module.note_credentials_accepted(client)

    try:
        with pytest.raises(Exception):
            await _read(client)
    finally:
        client_module._HOST_RPC2.pop(key, None)

    assert credentials_accepted_since(client, mark)
    assert client_module._CREDENTIALS_REFUSED == {}


async def test_a_refused_login_is_recorded_against_the_sessions_password():
    """Not the caller's: an entry with another password riding on the session
    is not the one the device turned down."""
    current = DahuaClient(USER, PASSWORD, ADDRESS, 80, 554, None)
    outdated = DahuaClient(USER, "old", ADDRESS, 80, 554, None)
    key = _session_that_refuses(
        current,
        client_module.Rpc2MethodRefused(
            "login refused", method=client_module.RPC2_LOGIN_METHOD
        ),
    )

    try:
        with pytest.raises(Exception):
            await _read(outdated)
    finally:
        client_module._HOST_RPC2.pop(key, None)

    assert (
        client_module.credentials_identity(current)
        in client_module._CREDENTIALS_REFUSED
    )
    assert (
        client_module.credentials_identity(outdated)
        not in client_module._CREDENTIALS_REFUSED
    )
