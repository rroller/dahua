"""Giving back a shared RPC2 session, and the two table shapes a write has to handle.

One login is shared by every channel on a host, so releasing it is reference counted and
the last one out has to take it down cleanly. None of that teardown was executed, and it
runs at exactly the moment things are already going wrong: a reload, a device that has
rebooted, a network that has gone away.

The contracts that matter here are all "one failure must not strand the next step":

* **A device that will not accept the logout must still have its socket closed.** Losing
  the courtesy of a logout costs nothing, because the device times the session out on its
  own; leaking the aiohttp session leaks a socket on every reload.
* **The keepalive is stopped before the logout**, and its cancellation is awaited, so it
  cannot fire one more request against a session that is being closed.
* **A transport failure on a config read drops the shared login and retries once.** Twice
  means RPC2 is not going to work, so it raises and the caller falls back to CGI. A
  refusal from the device is different and is not retried at all, because logging in
  again cannot change its mind about a table it does not serve.

`_rpc2_set_config_value` is read-modify-write because `setConfig` replaces the whole
table, and the table arrives in two shapes: a per-channel list on some tables and a bare
object on others (MotionDetect is a one-element list on an SL300, General is an object).
Both are handled, and a table that is neither raises rather than writing nothing and
reporting success.
"""

import asyncio

import pytest

from custom_components.dahua import client as client_module
from custom_components.dahua.client import DahuaClient, _release_rpc2

KEY = ("10.0.0.5:80", "admin")


@pytest.fixture(autouse=True)
def _no_shared_sessions():
    """The registry is module level, so a leak here would reach every other test."""
    client_module._HOST_RPC2.clear()
    yield
    client_module._HOST_RPC2.clear()


class _Rpc2Client:
    def __init__(self, logout_raises=False):
        self.logout_raises = logout_raises
        self.logouts = 0
        self._session_id = "session-1"

    async def logout(self):
        self.logouts += 1
        if self.logout_raises:
            raise RuntimeError("the device refused the logout")


class _Session:
    def __init__(self, closed=False):
        self.closed = closed
        self.closes = 0

    async def close(self):
        self.closes += 1
        self.closed = True


def _holder(*, refs=1, logout_raises=False, task="a-login", keepalive=None,
            session=None):
    holder = object.__new__(client_module._SharedRpc2Session)
    holder.session = session if session is not None else _Session()
    holder.client = _Rpc2Client(logout_raises)
    holder.task = task
    holder.refs = refs
    holder.keepalive = keepalive
    client_module._HOST_RPC2[KEY] = holder
    return holder


async def _forever():
    await asyncio.Event().wait()


# --- reference counting -----------------------------------------------------


async def test_releasing_a_key_that_is_not_there_is_quiet():
    """Teardown can run after setup failed, or twice, and neither is an error."""
    await _release_rpc2(("nobody", "nothing"))


async def test_another_channel_still_holding_it_keeps_the_session():
    """Eleven channels share one login. Closing it because one of them unloaded would
    take the other ten down with it."""
    holder = _holder(refs=3)

    await _release_rpc2(KEY)

    assert holder.refs == 2
    assert holder.session.closes == 0
    assert holder.client.logouts == 0
    assert client_module._HOST_RPC2.get(KEY) is holder, "it was dropped too early"


async def test_the_last_one_out_closes_it():
    holder = _holder(refs=1)

    await _release_rpc2(KEY)

    assert holder.client.logouts == 1
    assert holder.session.closes == 1
    assert KEY not in client_module._HOST_RPC2


async def test_the_registry_entry_goes_before_the_teardown():
    """So a channel arriving during the teardown starts a fresh login rather than
    taking a reference on a session being closed."""
    holder = _holder(refs=1)
    seen = []

    async def logout():
        seen.append(KEY in client_module._HOST_RPC2)

    holder.client.logout = logout

    await _release_rpc2(KEY)

    assert seen == [False], "the key was still registered during logout"


# --- the keepalive goes first ------------------------------------------------


async def test_the_keepalive_is_stopped_and_awaited():
    """Awaited, unlike cancellations elsewhere in this integration, because what
    follows depends on it having actually stopped."""
    keepalive = asyncio.ensure_future(_forever())
    holder = _holder(refs=1, keepalive=keepalive)

    await _release_rpc2(KEY)

    assert keepalive.cancelled() or keepalive.done()
    assert holder.keepalive is None


async def test_the_keepalive_is_stopped_before_the_logout():
    """Or it fires one more request at a session that is being closed."""
    order = []

    async def _watch():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            order.append("keepalive stopped")
            raise

    keepalive = asyncio.ensure_future(_watch())
    await asyncio.sleep(0)
    holder = _holder(refs=1, keepalive=keepalive)

    async def logout():
        order.append("logout")

    holder.client.logout = logout

    await _release_rpc2(KEY)

    assert order == ["keepalive stopped", "logout"], order


# --- a logout the device will not take --------------------------------------


async def test_a_logout_that_fails_still_closes_the_socket():
    """The one that leaks. A device that has rebooted will not know the session, and
    the socket has to go regardless."""
    holder = _holder(refs=1, logout_raises=True)

    await _release_rpc2(KEY)

    assert holder.client.logouts == 1
    assert holder.session.closes == 1, "the aiohttp session was leaked"
    assert KEY not in client_module._HOST_RPC2


async def test_a_session_that_was_never_logged_in_is_not_logged_out():
    """`task` is None when the login never happened, and asking a device to end a
    session it never issued is a request for nothing."""
    holder = _holder(refs=1, task=None)

    await _release_rpc2(KEY)

    assert holder.client.logouts == 0
    assert holder.session.closes == 1


async def test_an_already_closed_session_is_not_closed_again():
    holder = _holder(refs=1, session=_Session(closed=True))

    await _release_rpc2(KEY)

    assert holder.session.closes == 0


# --- a config read that fails on the way, not at the device -----------------


def _client():
    client = object.__new__(DahuaClient)
    client._address = "10.0.0.5"
    client._device = "10.0.0.5:80"
    client._username = "admin"
    return client


async def test_a_transport_failure_drops_the_login_and_tries_again():
    """An expired session shows up as a failure on the way out, and the answer is to
    log in again. Dropping `task` is what makes the next pass do that."""
    holder = _holder(refs=1)
    client = _client()
    attempts = []

    async def _shared_rpc2():
        attempts.append(holder.task)
        return holder

    async def get_config(payload):
        if len(attempts) == 1:
            raise OSError("connection reset")
        return {"table": {"Enable": "true"}}

    holder.client.get_config = get_config
    client._shared_rpc2 = _shared_rpc2

    await client._rpc2_get_config("MotionDetect")

    assert len(attempts) == 2, attempts
    assert attempts[1] is None, "the stale login was not dropped before the retry"


async def test_a_second_transport_failure_gives_up():
    """Twice means RPC2 is not going to work here, and the caller falls back to CGI.
    Retrying for ever would stall every poll behind a dead transport."""
    holder = _holder(refs=1)
    client = _client()
    tried = []

    async def _shared_rpc2():
        tried.append(1)
        return holder

    async def get_config(payload):
        raise OSError("connection reset")

    holder.client.get_config = get_config
    client._shared_rpc2 = _shared_rpc2

    with pytest.raises(OSError):
        await client._rpc2_get_config("MotionDetect")

    assert len(tried) == 2, "it should try exactly twice"


# --- the two table shapes a write has to handle -----------------------------


def _writer(response):
    """A client whose shared session answers one getConfig and records the setConfig."""
    holder = _holder(refs=1)
    client = _client()
    written = []

    async def _shared_rpc2():
        return holder

    async def get_config(payload):
        return response

    async def set_configs(pairs):
        written.append(pairs)

    holder.client.get_config = get_config
    holder.client.set_configs = set_configs
    client._shared_rpc2 = _shared_rpc2
    return client, written


async def test_a_per_channel_table_is_written_at_its_channel():
    """Read-modify-write, because setConfig replaces the whole table: a table built
    from scratch would drop every setting this code does not know about."""
    client, written = _writer({"table": [{"Enable": "false"}, {"Enable": "false"}]})

    await client._rpc2_set_config_value("MotionDetect", 1, "Enable", "true")

    (name, table), = written[0]
    assert name == "MotionDetect"
    assert table[1]["Enable"] == "true"
    assert table[0]["Enable"] == "false", "the other channel was changed too"


async def test_a_bare_object_table_is_written_directly():
    """General is an object rather than a list, so both shapes are handled rather
    than one being assumed."""
    client, written = _writer({"table": {"MachineName": "old"}})

    await client._rpc2_set_config_value("General", 0, "MachineName", "new")

    (_name, table), = written[0]
    assert table == {"MachineName": "new"}


async def test_a_table_nested_under_params_is_still_found():
    """Older firmware nests it. Reading only the top level would find no table and
    raise on a device that answered perfectly well."""
    client, written = _writer({"params": {"table": {"MachineName": "old"}}})

    await client._rpc2_set_config_value("General", 0, "MachineName", "new")

    (_name, table), = written[0]
    assert table == {"MachineName": "new"}


async def test_no_table_at_all_raises_rather_than_reporting_success():
    """The alternative is a write that silently changes nothing and returns
    `{"result": True}`, which is the worst possible answer."""
    client, _written = _writer({"params": {}})

    with pytest.raises(ConnectionError, match="no General table"):
        await client._rpc2_set_config_value("General", 0, "MachineName", "new")


async def test_a_channel_the_table_does_not_have_raises():
    client, _written = _writer({"table": [{"Enable": "false"}]})

    with pytest.raises(ConnectionError, match="no channel 4"):
        await client._rpc2_set_config_value("MotionDetect", 4, "Enable", "true")


async def test_a_successful_write_reports_success():
    client, written = _writer({"table": {"MachineName": "old"}})

    assert await client._rpc2_set_config_value(
        "General", 0, "MachineName", "new") == {"result": True}
    assert len(written) == 1


# --- and the flag ----------------------------------------------------------


@pytest.mark.parametrize("enabled", [True, False])
def test_use_rpc2_surfaces_the_flag(enabled):
    """A property, not a method. Reading it off an instance is the only way to get the
    value: off the class it is the descriptor, which is truthy whatever the flag says."""
    client = object.__new__(DahuaClient)
    client._use_rpc2 = enabled

    assert client.use_rpc2 is enabled
