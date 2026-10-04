"""The shared RPC2 session expires on a timer, and every caller has to survive it.

Measured on a DHI-NVR5464-16P-EI carrying twelve channels: the recorder ends a
session about every thirty-one minutes whatever the keepalive does, so this is
not a fault to be prevented, it is a condition to be handled. Two of the ten
callers handled it and eight did not. The one that mattered was the remote IVS
read, which runs for every channel on every poll:

    coordinator.py  _async_update_data
    client.py       async_get_remote_ivs_rules
    rpc2.py         async_get_remote_ivs_rules
    rpc2.py         request
    Rpc2MethodRefused: configManager.getConfig returned result=false
                       (code=287637504, message=session is out of date!)

Sixteen episodes in six hours on that box, every entity on the host unavailable
for up to fourteen seconds each time, and an ERROR in the log for each one.

The RPC2-side method only logs in when `_session_id` is falsy, and an *expired*
session still has one, so nothing on that path ever asked for a new login. The
last test here is the one that keeps this fixed: it reads the source and fails if
a new caller of the shared session appears that is neither routed through the
helper nor on the list of frames that handle expiry themselves.
"""

import ast
import inspect
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.dahua import client as client_module
from custom_components.dahua.client import DahuaClient
from custom_components.dahua.rpc2 import Rpc2MethodRefused

EXPIRED = Rpc2MethodRefused(
    "expired", code=287637504, message="session is out of date!"
)

# What the recorder really answers for a table it does not serve. It must not be
# mistaken for an expired session: retrying spends a login to learn nothing.
ABSENT = Rpc2MethodRefused("absent", code=285278249, message="Authority:check failure.")


class FakeRpc2:
    """Counts logins and records which session each call was made on."""

    logins = 0
    failures = []
    calls = []

    def __init__(self, *args):
        self._session_id = None
        self._ptz_objects = {}

    async def login(self):
        type(self).logins += 1
        self._session_id = "session-%d" % type(self).logins
        return {"params": {"keepAliveInterval": 60}}

    async def logout(self):
        return True

    def _record(self, name):
        type(self).calls.append((name, self._session_id))
        if type(self).failures:
            raise type(self).failures.pop(0)

    async def request(self, method=None, **kwargs):
        self._record(method)
        return {"result": True}

    async def get_config(self, params):
        self._record("get_config:%s" % params["name"])
        return {"table": [{"Enable": True}]}

    async def get_product_definition(self, name):
        self._record("get_product_definition")
        return {"MaxExtraStream": 2}

    async def async_get_remote_ivs_rules(self, channel):
        self._record("remote_ivs_read:%d" % channel)
        return [{"Name": "rule", "Enable": True}]

    async def async_set_remote_ivs_rule_by_id(self, channel, rule_id, enabled):
        self._record("remote_ivs_write:%d:%s:%s" % (channel, rule_id, enabled))
        return None

    async def set_configs(self, configs):
        self._record("set_configs:%s" % ",".join(name for name, _table in configs))
        return {"result": True}


@pytest.fixture
async def rpc2_client():
    FakeRpc2.logins = 0
    FakeRpc2.failures = []
    FakeRpc2.calls = []
    client_module._HOST_RPC2.clear()
    client_module._HOST_REMOTE_IVS_LOCKS.clear()
    client = DahuaClient("u", "p", "recorder", 80, 554, None, use_rpc2=True)
    with patch.object(client_module, "DahuaRpc2Client", FakeRpc2), patch.object(
        DahuaClient, "_new_rpc2_session", return_value=AsyncMock()
    ):
        yield client
        await client.close()
    client_module._HOST_RPC2.clear()
    client_module._HOST_REMOTE_IVS_LOCKS.clear()


# The call sites that had no recovery at all before this change. Each entry is
# what to call, and the call it is expected to make on the RPC2 client.
CALLERS = {
    "remote_ivs_read": (
        lambda client: client.async_get_remote_ivs_rules(3),
        "remote_ivs_read:3",
    ),
    "remote_ivs_write": (
        lambda client: client.async_set_remote_ivs_rule_by_id(3, "rule-1", True),
        "remote_ivs_write:3:rule-1:True",
    ),
    "product_definition": (
        lambda client: client.async_get_product_definition_rpc2("MaxExtraStream"),
        "get_product_definition",
    ),
}


@pytest.mark.parametrize("caller", sorted(CALLERS))
async def test_an_expired_session_is_renewed_and_the_call_retried(rpc2_client, caller):
    """The refusal used to propagate. Now it costs one login and one retry."""
    call, expected = CALLERS[caller]
    FakeRpc2.failures = [EXPIRED]

    await call(rpc2_client)

    assert FakeRpc2.logins == 2
    assert FakeRpc2.calls == [(expected, "session-1"), (expected, "session-2")]


@pytest.mark.parametrize("caller", sorted(CALLERS))
async def test_a_second_expiry_is_not_retried_again(rpc2_client, caller):
    """Two in a row is not an expiry, and a login per attempt is a loop."""
    call, _expected = CALLERS[caller]
    FakeRpc2.failures = [EXPIRED, EXPIRED]

    with pytest.raises(Rpc2MethodRefused):
        await call(rpc2_client)

    assert FakeRpc2.logins == 2
    assert len(FakeRpc2.calls) == 2


@pytest.mark.parametrize("caller", sorted(CALLERS))
async def test_a_table_the_device_does_not_serve_is_not_retried(rpc2_client, caller):
    """`Authority:check failure` means no such name on this recorder. Logging in
    again cannot change the device's mind."""
    call, _expected = CALLERS[caller]
    FakeRpc2.failures = [ABSENT]

    with pytest.raises(Rpc2MethodRefused) as caught:
        await call(rpc2_client)

    assert caught.value.code == 285278249
    assert FakeRpc2.logins == 1
    assert len(FakeRpc2.calls) == 1


async def test_the_stale_login_is_torn_down_not_just_retried(rpc2_client):
    """A retry on the same session would be refused the same way. The point of
    the teardown is that the second attempt runs on a session that is new."""
    FakeRpc2.failures = [EXPIRED]
    holder = await rpc2_client._shared_rpc2()
    first_keepalive = holder.keepalive

    await rpc2_client.async_get_remote_ivs_rules(3)

    assert first_keepalive.cancelled()
    assert holder.keepalive is not first_keepalive
    assert holder.keepalive is not None and not holder.keepalive.done()


async def test_a_login_another_caller_already_replaced_is_left_alone(rpc2_client):
    """Twelve channels meet the same expired session within milliseconds. The
    first replaces the login; the rest must not tear down a replacement they
    never used, which would turn one expiry into twelve."""
    await rpc2_client._shared_rpc2()
    holder = client_module._HOST_RPC2[rpc2_client._rpc2_key()]
    stale_task = holder.task

    await client_module.drop_stale_rpc2_login(holder, stale_task)
    assert holder.task is None

    replacement = await rpc2_client._shared_rpc2()
    fresh_task = replacement.task
    assert fresh_task is not stale_task

    # The straggler returns now, still holding the task it started with.
    await client_module.drop_stale_rpc2_login(replacement, stale_task)

    assert replacement.task is fresh_task
    assert replacement.keepalive is not None and not replacement.keepalive.done()


async def test_a_write_that_meets_an_expiry_reads_its_table_again(rpc2_client):
    """A read-modify-write is one retryable action, not two.

    configManager.setConfig replaces the whole table, so what gets written is
    built from a table that was just read. If the session dies between the read
    and the write, that table was read on a session the device has already
    forgotten, so the retry has to read it again rather than put the old one
    back.

    Not hypothetical: routing only the read through the helper left the write
    still reaching for a `holder` no longer bound, which is a NameError on the
    first config write after any expiry.
    """
    writes = []

    async def set_configs_expiring_once(self, configs):
        self._record("set_configs:%s" % ",".join(name for name, _t in configs))
        writes.append(configs)
        if len(writes) == 1:
            raise EXPIRED
        return {"result": True}

    with patch.object(FakeRpc2, "set_configs", set_configs_expiring_once):
        await rpc2_client._rpc2_set_config_value("MotionDetect", 0, "Mode", "Manual")

    assert [name for name, _session in FakeRpc2.calls] == [
        "get_config:MotionDetect",
        "set_configs:MotionDetect",
        "get_config:MotionDetect",
        "set_configs:MotionDetect",
    ]
    # The retry ran on the new session, not the one that had expired.
    assert [session for _name, session in FakeRpc2.calls] == [
        "session-1",
        "session-1",
        "session-2",
        "session-2",
    ]
    # And it carried the value asked for, from a table read on the live session.
    assert writes[1][0][1][0]["Mode"] == "Manual"


# --- the part that keeps it fixed ---------------------------------------------

# Frames allowed to take the shared session without going through the helper.
# Each handles expiry itself, and the reason is what keeps this list short.
HANDLES_EXPIRY_ITSELF = {
    # The helper. It is the thing being excepted from itself.
    "_rpc2_shared_call",
    # allow_missing has to tell an absent table apart from an expired session,
    # and only this frame knows which read asked for which.
    "_rpc2_get_config",
    # Drops the login on any exception, not only a refusal, because a camera
    # reboot ends this read without ever answering.
    "async_get_uptime_last",
    # The event stream is a long-lived attach, not a request: it recovers by
    # reattaching, and a silent retry here would hide a closed stream.
    "_stream_events_rpc2",
}


def _frames_taking_the_shared_session():
    source = inspect.getsource(client_module)
    tree = ast.parse(source)
    owner = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for line in range(node.lineno, (node.end_lineno or node.lineno) + 1):
                owner.setdefault(line, node.name)
    found = set()
    for number, line in enumerate(source.splitlines(), 1):
        if "_shared_rpc2()" in line:
            found.add(owner.get(number, "<module>"))
    return found


def test_every_caller_of_the_shared_session_is_accounted_for():
    """A new caller that takes the holder directly is the bug this PR fixes,
    reintroduced. It cannot be caught by running the code -- it only shows when a
    real device ends a real session -- so it is caught by reading the source."""
    frames = _frames_taking_the_shared_session()

    # The scan really found something; an empty set would pass vacuously.
    assert "_rpc2_shared_call" in frames

    assert (
        frames == HANDLES_EXPIRY_ITSELF
    ), "these take the shared RPC2 session without expiry handling: %s" % sorted(
        frames - HANDLES_EXPIRY_ITSELF
    )
