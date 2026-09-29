"""NVR IVS writes address one field, and resolve the rule ID every time.

The write used to post the whole channel table back. A DHI-NVR5464 refuses that
as too long, `errCode 287638033 "Request length error!"`, because one channel is
9KB to 22KB of rules, so every switch on that recorder was inert. Addressing the
field makes it 91 bytes and the same recorder accepts it.

The fresh per-channel read stays, and matters more than before: it resolves the
ID to an index, and that index is now what addresses the write.
"""

import asyncio
import re
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.dahua import client as client_module
from custom_components.dahua.client import DahuaClient, flatten_rpc2_config
from custom_components.dahua.ivs import ivs_rules_for_channel
from custom_components.dahua.rpc2 import DahuaRpc2Client


def rule(rule_id, enabled, name="IVS-1"):
    return {
        "Id": rule_id, "Class": "Normal", "Name": name,
        "Type": "CrossLineDetection", "Enable": enabled,
        "Points": [{"X": 12, "Y": 34}],
    }


async def test_remote_write_resolves_id_then_writes_only_that_field():
    """Id 1 sits at index 1 here, so the resolved index is what must be addressed.

    Sibling rules are now preserved by construction rather than by careful
    copying: nothing but the one boolean is sent, so there is no table to get
    wrong. The whole-table form this replaces is refused outright by a
    DHI-NVR5464, which rejects the 9KB to 22KB body as too long.
    """
    rpc = object.__new__(DahuaRpc2Client)
    current = [rule(2, True, "IVS-2"), rule(1, True)]
    rpc.async_get_remote_ivs_rules = AsyncMock(return_value=current)
    rpc.request = AsyncMock(return_value={"result": True})

    await rpc.async_set_remote_ivs_rule_by_id(10, "1", False)

    rpc.async_get_remote_ivs_rules.assert_awaited_once_with(10)
    rpc.request.assert_awaited_once_with(
        method="configManager.setConfig",
        params={
            "name": "RemoteVideoAnalyseRule[10][1].Enable",
            "table": False,
            "options": [], "channel": 10,
        },
    )
    assert current[1]["Enable"] is True
    assert current[0]["Points"] == [{"X": 12, "Y": 34}]


@pytest.mark.parametrize("table", [[], [rule(2, True)], [rule(1, True), rule(1, False)]])
async def test_missing_or_ambiguous_remote_rule_never_writes(table):
    rpc = object.__new__(DahuaRpc2Client)
    rpc.async_get_remote_ivs_rules = AsyncMock(return_value=table)
    rpc.request = AsyncMock()
    with pytest.raises(ValueError):
        await rpc.async_set_remote_ivs_rule_by_id(10, "1", False)
    rpc.request.assert_not_awaited()


def test_remote_discovery_uses_zero_based_channel_and_normal_class():
    table = flatten_rpc2_config(
        "RemoteVideoAnalyseRule", [rule(1, True), rule(2, False, "IVS-2")],
        "table.RemoteVideoAnalyseRule[10]",
    )
    assert [r["id"] for r in ivs_rules_for_channel(table, 10, "RemoteVideoAnalyseRule")] == ["1", "2"]
    assert ivs_rules_for_channel(table, 11, "RemoteVideoAnalyseRule") == []


async def test_initial_nvr_discovery_uses_the_per_channel_remote_read(hass):
    """Initial discovery must use the same per-channel RPC2 shape as the switch.

    The exact RPC2 contract is pinned below by
    test_remote_read_contract_uses_zero_based_channel_and_only_local_false.
    This test pins the other half of the chain: coordinator setup must reach
    that method with the current channel rather than discovering rules through
    a different whole-table read shape.
    """
    from tests.dahua.test_probe_timeouts import _Client, _coordinator

    channel = 10
    table = flatten_rpc2_config(
        "RemoteVideoAnalyseRule",
        [rule(1, True), rule(2, False, "IVS-2")],
        f"table.RemoteVideoAnalyseRule[{channel}]",
    )
    client = _Client()
    client.async_get_remote_ivs_rules = AsyncMock(return_value=table)

    c = _coordinator(client)
    c.hass = hass
    c._channel = channel
    c._channel_number = channel + 1
    c.is_nvr_channel = lambda: True

    await c._async_update_data()

    client.async_get_remote_ivs_rules.assert_awaited_once_with(channel)
    assert [item["id"] for item in c.get_ivs_rules()] == ["1", "2"]
    assert all(item.get("remote") is True for item in c.get_ivs_rules())


async def test_remote_read_contract_uses_zero_based_channel_and_only_local_false():
    rpc = object.__new__(DahuaRpc2Client)
    rpc._session_id = None
    rpc.login = AsyncMock()
    table = [rule(1, True)]
    rpc.get_config = AsyncMock(return_value={"table": table})

    assert await rpc.async_get_remote_ivs_rules(10) == table
    rpc.login.assert_awaited_once_with()
    rpc.get_config.assert_awaited_once_with({
        "name": "RemoteVideoAnalyseRule", "onlyLocal": False, "channel": 10,
    })


@pytest.mark.parametrize("error", [ConnectionError, ValueError, TimeoutError])
async def test_unsupported_remote_ivs_does_not_fail_entry_setup(error, hass):
    from tests.dahua.test_probe_timeouts import _Client, _coordinator

    c = _coordinator(_Client(failing=["async_get_remote_ivs_rules"], error=error))
    c.hass = hass
    c.is_nvr_channel = lambda: True
    await c._async_update_data()

    assert c.initialized
    assert c.get_ivs_rules() == []


async def test_remote_poll_reads_channel_table_only_when_switches_enabled(hass):
    from tests.dahua.test_poll_skips_unused import _coordinator

    table = flatten_rpc2_config(
        "RemoteVideoAnalyseRule", [rule(1, False)], "table.RemoteVideoAnalyseRule[10]",
    )
    for enabled in (True, False):
        c = _coordinator(switch=enabled)
        c.hass = hass
        c._channel = 10
        c.is_nvr_channel = lambda: True
        c._ivs_rules = ivs_rules_for_channel(table, 10, "RemoteVideoAnalyseRule")
        c.client.async_get_remote_ivs_rules = AsyncMock(return_value=table)
        c.client.async_get_uptime_last = AsyncMock(return_value=60)

        data = await c._async_update_data()

        assert c.client.async_get_remote_ivs_rules.await_count == int(enabled)
        if enabled:
            c.client.async_get_remote_ivs_rules.assert_awaited_once_with(10)
            assert data["table.RemoteVideoAnalyseRule[10][0].Enable"] == "false"


def _client(rpc, username="u"):
    client = DahuaClient(username, "p", "10.0.0.5", 80, 554, object())
    client._shared_rpc2 = AsyncMock(return_value=SimpleNamespace(client=rpc))
    return client


async def test_concurrent_remote_writes_to_one_channel_preserve_both_changes():
    table = [rule(1, True), rule(2, True, "IVS-2")]
    rpc = object.__new__(DahuaRpc2Client)
    rpc._session_id = "session"

    async def get_config(_params):
        snapshot = deepcopy(table)
        await asyncio.sleep(0.01)
        return {"table": snapshot}

    async def request(*, method, params):
        assert method == "configManager.setConfig"
        # The device applies a single addressed field, so the fake must too.
        # Under the old whole-table write this is where a second caller's change
        # could be clobbered by a stale snapshot; the per-field write removes
        # that class of race rather than relying on the lock alone.
        addressed = re.fullmatch(
            r"RemoteVideoAnalyseRule\[10\]\[(\d+)\]\.Enable", params["name"])
        assert addressed, params["name"]
        table[int(addressed[1])]["Enable"] = params["table"]
        return {"result": True}

    rpc.get_config = get_config
    rpc.request = request
    # Different entries, even with different credentials, control one recorder.
    first, second = _client(rpc, "u1"), _client(rpc, "u2")
    await asyncio.gather(
        first.async_set_remote_ivs_rule_by_id(10, "1", False),
        second.async_set_remote_ivs_rule_by_id(10, "2", False),
    )

    assert [row["Enable"] for row in table] == [False, False]
    assert table[0]["Points"] == [{"X": 12, "Y": 34}]


@pytest.mark.parametrize("write", [False, True])
async def test_remote_requests_share_the_host_request_limit(write):
    active = peak = 0

    async def request(*_args):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return [rule(1, True)]

    rpc = SimpleNamespace(
        async_get_remote_ivs_rules=request,
        async_set_remote_ivs_rule_by_id=request,
    )
    clients = [_client(rpc) for _ in range(8)]
    if write:
        await asyncio.gather(*(
            c.async_set_remote_ivs_rule_by_id(i, "1", False)
            for i, c in enumerate(clients)
        ))
    else:
        await asyncio.gather(*(
            c.async_get_remote_ivs_rules(i) for i, c in enumerate(clients)
        ))

    assert peak == client_module.MAX_CONCURRENT_REQUESTS_PER_HOST


@pytest.mark.parametrize("write", [False, True])
async def test_remote_requests_time_out_when_the_device_stalls(monkeypatch, write):
    monkeypatch.setattr(client_module, "TIMEOUT_SECONDS", 0.01)

    async def stalled(*_args):
        await asyncio.Event().wait()

    rpc = SimpleNamespace(
        async_get_remote_ivs_rules=stalled,
        async_set_remote_ivs_rule_by_id=stalled,
    )
    client = _client(rpc)
    with pytest.raises(TimeoutError):
        if write:
            await client.async_set_remote_ivs_rule_by_id(10, "1", False)
        else:
            await client.async_get_remote_ivs_rules(10)
