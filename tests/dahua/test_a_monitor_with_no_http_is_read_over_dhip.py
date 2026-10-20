"""RPC2 over DHIP, for an indoor monitor that serves no HTTP at all (#949).

A VTH5221D has only 5000 and 37777 open. There is no `/RPC2` to POST to, so
every read an entry makes has to go down the DHIP socket that `vto.py` already
speaks to doorbells. The envelope is the same on both: `DahuaRpc2Client.request`
builds `{id, method, params, session}` and POSTs it, and `DhipSession` writes
that same dict with a frame around it. So the client does not need a second
copy of every method, only a different way of sending one.

Three things here are measured on the hardware rather than assumed, and each
has a test because each would otherwise be an intermittent fault:

* **Two calls at once raise.** On a bare session, `asyncio.gather` of two calls
  gives `RuntimeError: read() called while another coroutine is already waiting
  for incoming data`, straight from the StreamReader. The coordinator fans its
  reads out with `gather`, so the transport serialises them.
* **A socket failure must read as transient.** `rpc2_failure_is_permanent` in
  client.py treats anything that is not a TimeoutError or an
  `aiohttp.ClientConnectionError` as proof the device does not speak RPC2, for
  the life of the process. `ConnectionRefusedError` is an OSError, so reported
  as itself it would retire the only transport this device has, for ever, on
  one dropped socket.
* **A refused login has to be able to stop the transport.** `General` on
  3.000.0012000.0.R reads `LockLoginEnable: true, LockLoginTimes: 5,
  LoginFailLockTime: 300`. Five refusals lock the account for five minutes,
  including for whoever is trying to correct the password. That is #729 on a
  third transport.

The verification against the real monitor, with these changes in place:
login succeeded with keepAliveInterval 60, `get_serial_number()` and
`get_config()` answered unchanged, three concurrent `request()` calls all
returned in 0.03s, and a refused table still raised `Rpc2MethodRefused` with
code 268959743.
"""

from __future__ import annotations

import asyncio
import json
from hashlib import md5
from types import SimpleNamespace

import aiohttp
import pytest

from custom_components.dahua import dhip as dhip_module
from custom_components.dahua.dhip import (
    DhipLoginRefused,
    DhipSession,
    DhipTransport,
)
from custom_components.dahua.client import DahuaClient
from custom_components.dahua.rpc2 import DahuaRpc2Client, Rpc2MethodRefused


class _FakeSession:
    """Stands in for a DhipSession: the socket, not the transport under test."""

    def __init__(self, session_id=4242, answer=None, fail_with=None, refuse=None):
        self.session_id = session_id
        self.sent: list[dict] = []
        self.closed = 0
        self._answer = answer
        self._fail_with = fail_with
        self._refuse = refuse
        self.concurrent = 0
        self.max_concurrent = 0

    async def login(self, username, password):
        if self._refuse is not None:
            self.session_id = 0
            raise self._refuse
        return {
            "result": True,
            "session": self.session_id,
            "params": {"keepAliveInterval": 60},
        }

    async def exchange(self, message):
        self.concurrent += 1
        self.max_concurrent = max(self.max_concurrent, self.concurrent)
        try:
            # A real socket round trip yields; without this the lock would look
            # like it works simply because nothing ever interleaves.
            await asyncio.sleep(0)
            self.sent.append(dict(message))
            if self._fail_with is not None:
                raise self._fail_with
            if callable(self._answer):
                return self._answer(message)
            return self._answer or {"id": message.get("id"), "result": True}
        finally:
            self.concurrent -= 1

    async def close(self):
        self.closed += 1


@pytest.fixture
def connections(monkeypatch):
    """Hand out fake sockets, leaving the real `_ensure_session` to run.

    Only the two things that touch the network are replaced: opening the
    connection, and the DhipSession built around it. Everything the tests are
    about -- the lock, the reconnect, the refusal budget, the generation
    counter, the exception reshaping -- is the real code.
    """
    queue: list = []

    async def open_connection(host, port):
        if not queue:
            raise AssertionError("asked for more connections than the test provided")
        nxt = queue[0]
        if isinstance(nxt, Exception) and not isinstance(nxt, DhipLoginRefused):
            queue.pop(0)
            raise nxt
        return object(), object()

    def build_session(reader, writer):
        nxt = queue.pop(0)
        if isinstance(nxt, DhipLoginRefused):
            return _FakeSession(session_id=0, refuse=nxt)
        return nxt

    monkeypatch.setattr(dhip_module.asyncio, "open_connection", open_connection)
    monkeypatch.setattr(dhip_module, "DhipSession", build_session)
    return queue


def _transport_over(connections, sessions, **kwargs):
    connections.extend(sessions)
    return DhipTransport("10.0.0.230", "admin", "secret", **kwargs)


# --- the measured concurrency failure ---------------------------------------


async def test_two_calls_at_once_do_not_overlap(connections):
    """The regression test for the RuntimeError the hardware raised.

    Removing the lock in `exchange` lets both calls into the session at once
    and `max_concurrent` reaches 2, which on a real StreamReader is the
    RuntimeError.
    """
    session = _FakeSession()
    transport = _transport_over(connections, [session])

    await asyncio.gather(
        transport.exchange({"id": 1, "method": "magicBox.getDeviceType"}),
        transport.exchange({"id": 2, "method": "magicBox.getSerialNo"}),
        transport.exchange({"id": 3, "method": "magicBox.getHardwareVersion"}),
    )

    assert session.max_concurrent == 1
    assert [sent["method"] for sent in session.sent] == [
        "magicBox.getDeviceType",
        "magicBox.getSerialNo",
        "magicBox.getHardwareVersion",
    ]


async def test_serialising_does_not_lose_a_call(connections):
    """The negative control for the lock: all three still get through."""
    session = _FakeSession()
    transport = _transport_over(connections, [session])

    replies = await asyncio.gather(
        *(
            transport.exchange({"id": n, "method": "magicBox.getSerialNo"})
            for n in (1, 2, 3)
        )
    )

    assert len(replies) == 3
    assert len(session.sent) == 3


# --- the session belongs to the socket --------------------------------------


def _real_session(session_id, reply):
    """A real DhipSession over a fake socket, so `exchange` itself is tested."""
    written: list[bytes] = []

    class _Writer:
        def write(self, data):
            written.append(data)

        async def drain(self):
            return None

    session = DhipSession.__new__(DhipSession)
    session._writer = _Writer()  # noqa: SLF001
    session._reader = None  # noqa: SLF001
    session._buffer = b""  # noqa: SLF001
    session._id = 0  # noqa: SLF001
    session.session_id = session_id

    async def _reply(request_id):
        return dict(reply, id=request_id)

    session._reply = _reply  # noqa: SLF001
    return session, written


def _envelope(written):
    """The JSON the session actually put on the wire, past the 32-byte header."""
    return json.loads(written[0][32:].decode("utf-8"))


async def test_the_live_session_replaces_what_the_caller_quoted():
    """A caller builds its envelope before the call, so after a reconnect the
    session id in it is one the device has already forgotten. The connection
    knows which session it holds, so it is the one that fills it in."""
    session, written = _real_session(99, {"result": True})

    await session.exchange({"id": 1, "method": "configManager.getConfig", "session": 7})

    assert _envelope(written)["session"] == 99


async def test_a_login_keeps_the_session_it_was_given():
    """global.login is the one call that must not have a session stamped on it,
    because it is what establishes one."""
    session, written = _real_session(0, {"result": True})

    await session.exchange(
        {"id": 1, "method": "global.login", "params": {}, "session": 0}
    )

    assert _envelope(written)["session"] == 0


async def test_the_envelope_carries_the_magic_the_device_expects():
    """vto.py sends it on every message and the monitors answer those, so it is
    part of the wire format rather than decoration."""
    session, written = _real_session(5, {"result": True})

    await session.exchange({"id": 3, "method": "magicBox.getSerialNo"})

    assert _envelope(written)["magic"] == "0x1234"


async def test_an_unnumbered_message_still_gets_an_id():
    """A reply is matched by id, so a message without one could never be
    answered."""
    session, written = _real_session(5, {"result": True})
    session._id = 11  # noqa: SLF001

    reply = await session.exchange({"method": "magicBox.getSerialNo"})

    assert _envelope(written)["id"] == 12
    assert reply["id"] == 12


async def test_the_sessions_own_counter_stays_ahead_of_the_callers():
    """`call` numbers its own messages. If `exchange` let the caller's higher id
    past without moving the counter, the next `call` would reuse it and read
    the wrong reply."""
    session, _written = _real_session(5, {"result": True})

    await session.exchange({"id": 40, "method": "magicBox.getSerialNo"})

    assert session._id == 40  # noqa: SLF001


# --- failures have to read as what they are ---------------------------------


async def test_a_refused_connection_is_reported_as_transient(connections):
    """`rpc2_failure_is_permanent` would otherwise retire the only transport
    this device has, for the life of the process, on one dropped socket."""
    transport = _transport_over(connections, [ConnectionRefusedError("no route")])

    with pytest.raises(aiohttp.ClientConnectionError):
        await transport.exchange({"id": 1, "method": "magicBox.getSerialNo"})


async def test_a_timeout_is_left_as_a_timeout(connections):
    """Already in the transient set, so it must not be wrapped into something
    else and lose that."""
    transport = _transport_over(connections, [TimeoutError("took too long")])

    with pytest.raises(TimeoutError):
        await transport.exchange({"id": 1, "method": "magicBox.getSerialNo"})


async def test_a_dead_socket_is_reconnected_once_and_the_call_retried(connections):
    """The first read after an idle spell is where a dropped connection shows
    up. One lost poll per drop would be a fault nobody could see."""
    dead = _FakeSession(fail_with=ConnectionResetError("peer went away"))
    alive = _FakeSession(answer={"id": 1, "result": True, "params": {"sn": "X"}})
    transport = _transport_over(connections, [dead, alive])

    reply = await transport.exchange({"id": 1, "method": "magicBox.getSerialNo"})

    assert reply["params"]["sn"] == "X"
    assert dead.closed == 1
    assert transport.generation == 2


async def test_a_second_failure_is_not_retried_for_ever(connections):
    first = _FakeSession(fail_with=ConnectionResetError("gone"))
    second = _FakeSession(fail_with=ConnectionResetError("still gone"))
    transport = _transport_over(connections, [first, second])

    with pytest.raises(aiohttp.ClientConnectionError):
        await transport.exchange({"id": 1, "method": "magicBox.getSerialNo"})

    assert transport.connected is False


# --- the lockout budget ------------------------------------------------------


async def test_a_refused_login_is_not_wrapped_as_a_transport_failure(connections):
    """A refusal is a statement about the credentials. Reported as a connection
    error it would be retried, which is what locks the device."""
    transport = _transport_over(
        connections, [DhipLoginRefused("User or password not valid!")]
    )

    with pytest.raises(DhipLoginRefused):
        await transport.exchange({"id": 1, "method": "magicBox.getSerialNo"})


async def test_the_budget_decides_whether_to_stop_trying(connections):
    """`LockLoginTimes: 5` with `LoginFailLockTime: 300` measured on 3.000. The
    transport does not know those numbers; the owner that holds the host's
    shared auth budget does, and says when to stop."""
    spent = []

    def refused(reason):
        spent.append(reason)
        return len(spent) >= 2

    transport = _transport_over(
        connections,
        [DhipLoginRefused("no"), DhipLoginRefused("no"), _FakeSession()],
        on_login_refused=refused,
    )

    for _ in range(2):
        with pytest.raises(DhipLoginRefused):
            await transport.exchange({"id": 1, "method": "magicBox.getSerialNo"})

    # The third connection is never taken, although one was offered.
    with pytest.raises(DhipLoginRefused):
        await transport.exchange({"id": 1, "method": "magicBox.getSerialNo"})
    assert len(spent) == 2


async def test_changed_credentials_let_it_try_again(connections):
    transport = _transport_over(
        connections,
        [DhipLoginRefused("no"), _FakeSession()],
        on_login_refused=lambda reason: True,
    )

    with pytest.raises(DhipLoginRefused):
        await transport.exchange({"id": 1, "method": "magicBox.getSerialNo"})

    transport.clear_refusals()
    reply = await transport.exchange({"id": 1, "method": "magicBox.getSerialNo"})

    assert reply["result"] is True


async def test_close_is_safe_twice(connections):
    session = _FakeSession()
    transport = _transport_over(connections, [session])
    await transport.exchange({"id": 1, "method": "magicBox.getSerialNo"})

    await transport.close()
    await transport.close()

    assert session.closed == 1
    assert transport.connected is False


# --- the client on top of it -------------------------------------------------


def _client(transport):
    # No aiohttp session at all. Anything that fell through to HTTP would raise
    # AttributeError here rather than quietly working in the test and failing
    # on a device that has no web server.
    return DahuaRpc2Client(
        "admin", "secret", "10.0.0.230", 80, 554, None, dhip_transport=transport
    )


async def test_the_client_reads_config_over_dhip(connections):
    session = _FakeSession(
        answer=lambda message: {
            "id": message["id"],
            "result": True,
            "params": {"table": {"SilenceEnable": False}},
        }
    )
    transport = _transport_over(connections, [session])

    params = await _client(transport).get_config({"name": "VideoTalkPhoneGeneral"})

    assert params["table"] == {"SilenceEnable": False}
    assert session.sent[0]["method"] == "configManager.getConfig"


async def test_a_refusal_still_arrives_as_a_refusal(connections):
    """Refusal learning reads `Rpc2MethodRefused`. Measured: AccessControl on
    this monitor answers result=false with code 268959743."""
    session = _FakeSession(
        answer=lambda message: {
            "id": message["id"],
            "result": False,
            "error": {"code": 268959743, "message": "Unknown error!"},
        }
    )
    transport = _transport_over(connections, [session])

    with pytest.raises(Rpc2MethodRefused) as refused:
        await _client(transport).get_config({"name": "AccessControl"})

    assert refused.value.code == 268959743


async def test_a_url_has_no_meaning_over_dhip(connections):
    """`/RPC2_Login` is an HTTP route. A caller that depends on a second
    endpoint should fail here rather than somewhere further away."""
    transport = _transport_over(connections, [_FakeSession()])

    with pytest.raises(ValueError, match="no separate URL"):
        await _client(transport).request("magicBox.getDeviceType", url="http://x/RPC2")


async def test_login_adopts_the_connections_session(connections):
    session = _FakeSession(session_id=777)
    transport = _transport_over(connections, [session])
    client = _client(transport)

    reply = await client.login()

    assert reply["session"] == 777
    assert reply["params"]["keepAliveInterval"] == 60
    assert client._session_id == 777  # noqa: SLF001


async def test_closing_the_client_closes_the_socket(connections):
    session = _FakeSession()
    transport = _transport_over(connections, [session])
    client = _client(transport)
    await client.login()

    await client.close()

    assert session.closed == 1
    assert client._session_id is None  # noqa: SLF001


# --- what the entry shows for a device with no magicBox CGI ------------------


def _cgi_absent_client(vth_identity, over_dhip=True, **extra):
    """A DahuaClient whose CGI reads 404, with a VTH identity behind them.

    `over_dhip` is how the device was added. True is the monitor this PR is
    about, which serves no HTTP at all. False is a VTH that does serve HTTP
    and answers 404 to magicBox.cgi only, which is kmich's VTH2421F-P: it has
    an entry today, and its identity must not move.
    """
    client = DahuaClient.__new__(DahuaClient)
    client._address = "10.0.0.230"  # noqa: SLF001
    client._rtsp_port = 554  # noqa: SLF001
    client._username = "admin"  # noqa: SLF001
    client._password = "secret"  # noqa: SLF001
    client._rpc2_over_dhip = over_dhip  # noqa: SLF001
    client.identity_derived_from_credentials = False

    async def get(url, verify_ok=False):
        raise aiohttp.ClientResponseError(None, (), status=404, message=url)

    async def identity():
        return vth_identity

    client.get = get
    client._vth_identity_over_rpc2 = identity  # noqa: SLF001
    for name, value in extra.items():
        setattr(client, name, value)
    return client


_VTH = {
    "type": "VTH5221D",
    "version": "3.000.0012000.0.R",
    "class": "VTH",
    "serial": "3E0164EPAN00095",
    "own_video": False,
}


async def test_a_monitor_reports_its_own_serial_rather_than_a_hash():
    """The #805 shape. Without this the serial is an md5 of the address, port,
    username and password, so changing the password makes the same monitor
    arrive as a new device. Measured: magicBox.getSerialNo answers
    3E0164EPAN00095 on 3.000.0012000.0.R."""
    client = _cgi_absent_client(_VTH)

    assert await client.async_get_system_info() == {"serialNumber": "3E0164EPAN00095"}
    assert client.identity_derived_from_credentials is False


async def test_a_monitor_is_named_by_its_model_rather_than_a_hash():
    client = _cgi_absent_client(_VTH)

    assert await client.get_machine_name() == {"name": "VTH5221D"}


async def test_a_monitor_that_will_not_give_a_serial_is_retried_not_hashed():
    """An answer that did not arrive is not an answer (#1057's shape).

    The hash was the obvious fallback and it is the wrong one here. Every
    entity `unique_id` is built from this read, so one boot where
    getSerialNo times out would give the whole entry a different identity and
    Home Assistant would build a second set of entities on `_2` names, which
    nothing later undoes. Raising instead reaches the coordinator as
    UpdateFailed, which on a first refresh is setup_retry, and the next poll
    can get it right.
    """
    client = _cgi_absent_client(dict(_VTH, serial=None))

    with pytest.raises(aiohttp.ClientResponseError):
        await client.async_get_system_info()

    assert client.identity_derived_from_credentials is False


async def test_a_serving_monitor_with_no_serial_is_retried_too():
    """The complement, and the reason the branch above is safe to add.

    A VTH that serves HTTP is in the same position since the gate came off
    (#1103 held it at the md5; migrate.py now moves the registry instead).
    It is still a monitor and still must not be hashed, because the hash is
    what builds a second set of entities on `_2` names.
    """
    client = _cgi_absent_client(dict(_VTH, serial=None), over_dhip=False)

    with pytest.raises(aiohttp.ClientResponseError):
        await client.async_get_system_info()

    assert client.identity_derived_from_credentials is False


async def test_a_monitor_that_serves_http_gets_its_real_serial_too():
    """The other half of what kmich reported on #1103.

    A VTH2421F-P serves HTTP on 80 and still answers 404 to magicBox.cgi, so
    it reaches the same branch as a monitor with no web server. #1103 held it
    at the md5 because its entities exist today and handing it the serial
    renames all of them. kmich also made the case for doing it properly: that
    generated id had already moved on him once, when he changed the password,
    which is #805. So the gate is gone and `migrate.py` moves the registry
    with it, under a backup, keeping every entity_id.
    """
    client = _cgi_absent_client(_VTH, over_dhip=False)

    assert await client.async_get_system_info() == {"serialNumber": "3E0164EPAN00095"}
    assert await client.get_machine_name() == {"name": "VTH5221D"}
    assert client.identity_derived_from_credentials is False
    # And it is not what it used to be, which is the thing the migration has
    # to recognise in the registry.
    assert client.credential_hash() == md5(b"10.0.0.230_554_admin_secret").hexdigest()


async def test_a_monitor_that_serves_http_still_gets_its_model_and_firmware():
    """Model, firmware and class shipped before #1103 and an HTTP VTH already
    read them over RPC2, so they were never gated and are not now."""
    client = _cgi_absent_client(_VTH, over_dhip=False)

    assert await client.get_device_type() == {"type": "VTH5221D"}
    assert await client.async_get_device_class() == "VTH"


async def test_anything_that_is_not_a_monitor_keeps_the_old_fallback():
    """The negative control. A camera with its CGI off must not start reporting
    a VTH's identity, and `_vth_identity_over_rpc2` returning None is how it
    says the device is not one."""
    client = _cgi_absent_client(None, over_dhip=False)

    info = await client.async_get_system_info()

    assert client.identity_derived_from_credentials is True
    assert len(info["serialNumber"]) == 32


async def test_a_refused_login_is_still_raised_rather_than_hashed():
    """strict_auth is the config flow deciding whether a password is right, and
    the VTH branch must not swallow a 401 on the way past."""
    client = _cgi_absent_client(_VTH)

    async def unauthorised(url, verify_ok=False):
        raise aiohttp.ClientResponseError(None, (), status=401, message="no")

    client.get = unauthorised

    with pytest.raises(aiohttp.ClientResponseError):
        await client.async_get_system_info(strict_auth=True)


# --- the read routing in client.py ------------------------------------------


def _dhip_routed_client(config_reply=None):
    """A DahuaClient pinned to DHIP, with the RPC2 config read stubbed."""
    client = DahuaClient.__new__(DahuaClient)
    client._address = "10.0.0.230"  # noqa: SLF001
    client._rpc2_over_dhip = True  # noqa: SLF001
    asked = []

    async def rpc2_get_config(table):
        asked.append(table)
        return config_reply or {"table.General.LocalNo": "8"}

    class _Slot:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *exc):
            return False

    client._rpc2_get_config = rpc2_get_config  # noqa: SLF001
    client._request_slot = _Slot  # noqa: SLF001
    return client, asked


async def test_a_config_read_goes_over_the_socket():
    client, asked = _dhip_routed_client()

    result = await client._request(  # noqa: SLF001
        "/cgi-bin/configManager.cgi?action=getConfig&name=General"
    )

    assert asked == ["General"]
    assert result == {"table.General.LocalNo": "8"}


@pytest.mark.parametrize(
    "url",
    [
        "/cgi-bin/snapshot.cgi?channel=1",
        "/cgi-bin/eventManager.cgi?action=attach&codes=[All]",
        "/cgi-bin/magicBox.cgi?action=getProductDefinition",
        "/cgi-bin/magicBox.cgi?action=getSystemInfo",
    ],
)
async def test_a_read_with_no_dhip_equivalent_is_a_404(url):
    """404 rather than something new, because `CAPABILITY_REFUSED` is
    `(400, 404, 501)` and the probes and refusals.py are already built to hear
    it and gate the entity off. Verified on the monitor: a snapshot, an event
    attach and a capability probe all come back 404."""
    client, asked = _dhip_routed_client()

    with pytest.raises(aiohttp.ClientResponseError) as refused:
        await client._request(url)  # noqa: SLF001

    assert refused.value.status == 404
    assert asked == []


async def test_a_read_pinned_to_cgi_is_refused_rather_than_rerouted():
    """The per-rule IVS read pins itself to CGI because it needs the array
    indexes only that table has. There is no such table here, and answering it
    from somewhere else would be worse than saying so."""
    client, asked = _dhip_routed_client()

    with pytest.raises(aiohttp.ClientResponseError) as refused:
        await client._request(  # noqa: SLF001
            "/cgi-bin/configManager.cgi?action=getConfig&name=VideoAnalyseRule",
            allow_rpc2=False,
        )

    assert refused.value.status == 404
    assert asked == []


# --- a refused table has to sound like an absent one ------------------------


def _refusing_client(refusal):
    """A DHIP-routed client whose RPC2 config read refuses."""
    client = DahuaClient.__new__(DahuaClient)
    client._address = "10.0.0.230"  # noqa: SLF001
    client._base = "http://10.0.0.230:80"  # noqa: SLF001
    client._rpc2_over_dhip = True  # noqa: SLF001

    async def rpc2_get_config(table):
        raise refusal

    class _Slot:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *exc):
            return False

    client._rpc2_get_config = rpc2_get_config  # noqa: SLF001
    client._request_slot = _Slot  # noqa: SLF001
    return client


MOTION = "/cgi-bin/configManager.cgi?action=getConfig&name=MotionDetect"


async def test_a_declined_table_arrives_as_the_404_every_caller_expects():
    """The bug that stopped a monitor ever finishing its first refresh.

    Over HTTP an absent table is a CGI 404, and the whole integration is built
    on hearing that: `async_get_config` swallows a ClientResponseError and
    returns {}, the probes judge on CAPABILITY_REFUSED, and the poll's fan-out
    has no `return_exceptions`. Over DHIP the device answers `result: false`,
    which arrived as `Rpc2MethodRefused` and escaped the gather.

    Measured on a VTH5221D: MotionDetect, DisableLinkage and DisableEventNotify
    are all declined with code 268959743.
    """
    client = _refusing_client(
        Rpc2MethodRefused("declined", code=268959743, message="Unknown error!")
    )

    with pytest.raises(aiohttp.ClientResponseError) as refused:
        await client._request(MOTION)  # noqa: SLF001

    assert refused.value.status == 404


async def test_that_404_can_actually_be_printed():
    """aiohttp's own __str__ reads `request_info.real_url`, so a None there
    turns every attempt to log this into an AttributeError raised from the
    logging call, nowhere near the read. The first probe to print one died on
    exactly that rather than on the refusal it was reporting."""
    client = _refusing_client(
        Rpc2MethodRefused("declined", code=268959743, message="Unknown error!")
    )

    with pytest.raises(aiohttp.ClientResponseError) as refused:
        await client._request(MOTION)  # noqa: SLF001

    rendered = str(refused.value)
    assert "404" in rendered
    assert "MotionDetect" in rendered


async def test_a_read_with_no_equivalent_can_be_printed_too():
    client, _asked = _dhip_routed_client()

    with pytest.raises(aiohttp.ClientResponseError) as refused:
        await client._request("/cgi-bin/snapshot.cgi?channel=1")  # noqa: SLF001

    assert "404" in str(refused.value)


async def test_a_stale_login_is_not_recorded_as_a_missing_table():
    """An expired session is not the device's verdict on the table. Converting
    it would teach the probes that a capability is absent because a session
    timed out, and that verdict outlives the session."""
    stale = Rpc2MethodRefused(
        "invalid session", code=287637505, message="Invalid session in request data!"
    )
    client = _refusing_client(stale)

    with pytest.raises(Rpc2MethodRefused):
        await client._request(MOTION)  # noqa: SLF001


async def test_unloading_the_entry_closes_its_shared_dhip_socket(connections):
    session = _FakeSession()
    connections.append(session)
    client = DahuaClient("u", "p", "10.0.0.230", 80, 554, None, rpc2_over_dhip=True)
    try:
        await client._rpc2_shared_call(lambda rpc: rpc.request("magicBox.getSerialNo"))
    finally:
        await client.close()
    assert session.closed == 1


async def test_shared_session_expiry_reconnects_the_dhip_socket(connections):
    def expired(message):
        if message["method"] == "magicBox.getSerialNo":
            return {
                "result": False,
                "error": {"code": 287637504, "message": "session is out of date!"},
            }
        return {"result": True}

    old = _FakeSession(session_id=41, answer=expired)
    new = _FakeSession(
        session_id=42, answer={"result": True, "params": {"sn": "stable"}}
    )
    connections.extend([old, new])
    client = DahuaClient("u", "p", "10.0.0.230", 80, 554, None, rpc2_over_dhip=True)
    try:
        result = await client._rpc2_shared_call(
            lambda rpc: rpc.request("magicBox.getSerialNo")
        )
        assert result["params"]["sn"] == "stable"
        assert old.closed == 1
    finally:
        await client.close()


@pytest.mark.parametrize(
    "method",
    ["accessControl.openDoor", "VideoTalkPhone.beginCall", "configManager.setConfig"],
)
async def test_a_lost_write_reply_does_not_replay_the_action(connections, method):
    first = _FakeSession(fail_with=ConnectionResetError("reply lost after write"))
    second = _FakeSession()
    transport = _transport_over(connections, [first, second])
    try:
        with pytest.raises(aiohttp.ClientConnectionError):
            await transport.exchange({"id": 1, "method": method})
        assert len(first.sent) == 1
        assert second.sent == []
        assert not transport.connected
    finally:
        await transport.close()


async def test_a_dhip_login_refusal_uses_the_coordinators_auth_error(connections):
    connections.append(DhipLoginRefused("wrong password"))
    client = DahuaClient("u", "p", "10.0.0.230", 80, 554, None, rpc2_over_dhip=True)
    try:
        with pytest.raises(aiohttp.ClientResponseError) as refused:
            await client._rpc2_shared_call(
                lambda rpc: rpc.request("magicBox.getSerialNo")
            )
        assert refused.value.status == 401
        assert "401" in str(refused.value)
    finally:
        await client.close()


async def test_missing_dhip_model_or_firmware_never_changes_the_serial_to_a_hash():
    client = _cgi_absent_client(None)
    with pytest.raises(aiohttp.ClientResponseError):
        await client.async_get_system_info()
    assert client.identity_derived_from_credentials is False


async def test_a_missing_serial_is_asked_again_on_the_same_client():
    client = _cgi_absent_client(None)
    client._vth_identity_over_rpc2 = DahuaClient._vth_identity_over_rpc2.__get__(client)
    serial_reads = 0

    async def request(method, **kwargs):
        nonlocal serial_reads
        if method == "magicBox.getSerialNo":
            serial_reads += 1
            if serial_reads == 1:
                raise TimeoutError("not ready")
            return {"params": {"sn": "stable-serial"}}
        if method == "magicBox.getDeviceClass":
            return {"params": {"type": "VTH"}}
        if method == "magicBox.getDeviceType":
            return {"params": {"type": "VTH5221D"}}
        if method == "magicBox.getSoftwareVersion":
            return {"params": {"version": {"Version": "3.000.0012000.0.R"}}}
        return {"params": {"table": {}}}

    async def shared(action):
        return await action(SimpleNamespace(request=request))

    client._rpc2_shared_call = shared
    with pytest.raises(aiohttp.ClientResponseError):
        await client.async_get_system_info()
    assert await client.async_get_system_info() == {"serialNumber": "stable-serial"}
    assert serial_reads == 2


@pytest.mark.parametrize("status", [404, 503, 401])
async def test_a_monitor_already_using_its_serial_cannot_revert_to_a_hash(status):
    client = _cgi_absent_client(None, over_dhip=False)
    client._require_reported_serial = True

    async def failed(url, verify_ok=False):
        raise aiohttp.ClientResponseError(None, (), status=status)

    client.get = failed
    with pytest.raises(aiohttp.ClientResponseError):
        await client.async_get_system_info()
    assert client.identity_derived_from_credentials is False
