"""NVR IVS writes preserve the complete channel table and stable rule IDs."""

import asyncio
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


async def test_remote_write_resolves_id_in_fresh_table_and_preserves_other_rules():
    rpc = object.__new__(DahuaRpc2Client)
    current = [rule(2, True, "IVS-2"), rule(1, True)]
    rpc.async_get_remote_ivs_rules = AsyncMock(return_value=current)
    rpc.request = AsyncMock(return_value={"result": True})

    await rpc.async_set_remote_ivs_rule_by_id(10, "1", False)

    rpc.async_get_remote_ivs_rules.assert_awaited_once_with(10)
    rpc.request.assert_awaited_once_with(
        method="configManager.setConfig",
        params={
            "name": "RemoteVideoAnalyseRule",
            "table": [current[0], {**current[1], "Enable": False}],
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
        table[:] = deepcopy(params["table"])
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
