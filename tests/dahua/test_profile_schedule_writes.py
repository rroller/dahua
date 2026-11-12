"""Profile Schedule writes preserve and verify the host-wide VideoInMode table."""

from datetime import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.dahua.client import DahuaClient


def _table(interval="1 05:30:00-15:30:00", mode=1):
    return [
        {
            "Config": [0, 1] if mode == 1 else [2],
            "Mode": mode,
            "TimeSection": [[interval, "0 00:00:00-00:00:00"]],
            "Future": {"keep": True},
        },
        {"OtherChannel": True},
    ]


def _client(*responses):
    rpc = SimpleNamespace(
        get_config=AsyncMock(side_effect=[{"table": value} for value in responses]),
        set_config=AsyncMock(),
    )
    client = object.__new__(DahuaClient)
    client._device = "camera"

    async def call_rpc2(callback):
        return await callback(rpc)

    client._rpc2_shared_call = AsyncMock(side_effect=call_rpc2)
    return client, rpc


async def test_schedule_write_is_whole_table_channel_owned_and_read_back():
    client, rpc = _client(_table(), _table("1 08:23:00-17:47:00"))
    result = await client.async_set_profile_schedule_times(0, time(8, 23), time(17, 47))
    written = rpc.set_config.await_args.args[1]
    assert written[0]["TimeSection"][0][0] == "1 08:23:00-17:47:00"
    assert written[0]["Config"] == [0, 1]
    assert written[0]["Future"] == {"keep": True}
    assert written[1] == {"OtherChannel": True}
    assert rpc.set_config.await_args.kwargs == {"channel": 0}
    assert result.day_start == time(8, 23)
    assert result.night_start == time(17, 47)


async def test_schedule_write_requires_schedule_before_set_config():
    client, rpc = _client(_table(mode=0))
    with pytest.raises(RuntimeError, match="Schedule mode"):
        await client.async_set_profile_schedule_times(0, time(8, 23), time(17, 47))
    rpc.set_config.assert_not_awaited()


async def test_success_reply_with_different_readback_fails_closed():
    client, _rpc = _client(_table(), _table("1 08:22:00-17:47:00"))
    with pytest.raises(RuntimeError, match="did not apply"):
        await client.async_set_profile_schedule_times(0, time(8, 23), time(17, 47))


async def test_missing_readback_table_fails_closed():
    rpc = SimpleNamespace(
        get_config=AsyncMock(side_effect=[{"table": _table()}, {}]),
        set_config=AsyncMock(),
    )
    client = object.__new__(DahuaClient)
    client._device = "camera"

    async def call_rpc2(callback):
        return await callback(rpc)

    client._rpc2_shared_call = AsyncMock(side_effect=call_rpc2)
    with pytest.raises(RuntimeError, match="did not apply"):
        await client.async_set_profile_schedule_times(0, time(8, 23), time(17, 47))


async def test_missing_channel_fails_before_set_config():
    client, rpc = _client(_table())
    with pytest.raises(ConnectionError, match="channel 3"):
        await client.async_set_profile_schedule_times(3, time(8, 23), time(17, 47))
    rpc.set_config.assert_not_awaited()
