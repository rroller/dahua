"""Tests for host-shared camera uptime and reboot generation detection."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from custom_components import dahua as dahua_module


def _coordinator(address, uptime):
    """Build the minimum coordinator shape required by the helper."""
    return SimpleNamespace(
        _address=address,
        client=SimpleNamespace(
            async_get_uptime_last=AsyncMock(side_effect=uptime)
            if isinstance(uptime, list)
            else AsyncMock(return_value=uptime)
        ),
    )


@pytest.fixture(autouse=True)
def _clear_host_uptime_state():
    """Do not let one test's host sample affect another."""
    dahua_module._HOST_UPTIME_STATE.clear()
    dahua_module._HOST_UPTIME_LOCKS.clear()

    yield

    dahua_module._HOST_UPTIME_STATE.clear()
    dahua_module._HOST_UPTIME_LOCKS.clear()


async def test_same_host_multiple_coordinators_share_one_uptime_read():
    """NVR channels polling together must not each hit RPC2."""
    first = _coordinator("10.0.0.1", 500)
    second = _coordinator("10.0.0.1", 500)

    with patch.object(
        dahua_module.time,
        "monotonic",
        side_effect=[100.0, 100.0, 100.1],
    ):
        first_generation = (
            await dahua_module._async_get_host_uptime_generation(first)
        )
        second_generation = (
            await dahua_module._async_get_host_uptime_generation(second)
        )

    assert first_generation == 0
    assert second_generation == 0

    first.client.async_get_uptime_last.assert_awaited_once()
    second.client.async_get_uptime_last.assert_not_awaited()


async def test_different_hosts_do_not_share_uptime_samples():
    first = _coordinator("10.0.0.1", 500)
    second = _coordinator("10.0.0.2", 700)

    with patch.object(
        dahua_module.time,
        "monotonic",
        side_effect=[
            100.0,
            100.0,
            100.1,
            100.1,
        ],
    ):
        await dahua_module._async_get_host_uptime_generation(first)
        await dahua_module._async_get_host_uptime_generation(second)

    first.client.async_get_uptime_last.assert_awaited_once()
    second.client.async_get_uptime_last.assert_awaited_once()

    assert set(dahua_module._HOST_UPTIME_STATE) == {
        "10.0.0.1",
        "10.0.0.2",
    }


async def test_uptime_rollback_increments_reboot_generation():
    coordinator = _coordinator(
        "10.0.0.1",
        [500, 12],
    )

    with patch.object(
        dahua_module.time,
        "monotonic",
        side_effect=[
            100.0,
            100.0,
            106.0,
            106.0,
        ],
    ):
        before = await dahua_module._async_get_host_uptime_generation(
            coordinator
        )
        after = await dahua_module._async_get_host_uptime_generation(
            coordinator
        )

    assert before == 0
    assert after == 1
    assert coordinator.client.async_get_uptime_last.await_count == 2

    state = dahua_module._HOST_UPTIME_STATE["10.0.0.1"]
    assert state["uptime"] == 12
    assert state["generation"] == 1


async def test_normal_uptime_increase_does_not_increment_generation():
    coordinator = _coordinator(
        "10.0.0.1",
        [500, 506],
    )

    with patch.object(
        dahua_module.time,
        "monotonic",
        side_effect=[
            100.0,
            100.0,
            106.0,
            106.0,
        ],
    ):
        first = await dahua_module._async_get_host_uptime_generation(
            coordinator
        )
        second = await dahua_module._async_get_host_uptime_generation(
            coordinator
        )

    assert first == 0
    assert second == 0


async def test_failed_uptime_read_is_deduped_for_same_poll_burst():
    coordinator = _coordinator("10.0.0.1", 500)
    coordinator.client.async_get_uptime_last = AsyncMock(
        side_effect=RuntimeError("RPC2 unavailable")
    )

    with patch.object(
        dahua_module.time,
        "monotonic",
        side_effect=[
            100.0,
            100.0,
            100.1,
        ],
    ):
        first = await dahua_module._async_get_host_uptime_generation(
            coordinator
        )
        second = await dahua_module._async_get_host_uptime_generation(
            coordinator
        )

    # Uptime support is optional; failure must not become a poll failure.
    assert first == 0
    assert second == 0

    # The second NVR-channel-style request is suppressed even though the
    # first host read failed.
    coordinator.client.async_get_uptime_last.assert_awaited_once()

    state = dahua_module._HOST_UPTIME_STATE["10.0.0.1"]
    assert state["uptime"] is None
    assert state["generation"] == 0
