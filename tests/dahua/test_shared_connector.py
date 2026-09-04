"""Channels of one NVR should share a connection pool, not open one each."""

import pytest

from custom_components import dahua as dahua_module
from custom_components.dahua import _acquire_connector, _release_connector


@pytest.fixture(autouse=True)
def _clean_registry():
    dahua_module._HOST_CONNECTORS.clear()
    yield
    dahua_module._HOST_CONNECTORS.clear()


async def test_entries_for_one_host_share_a_pool():
    a = _acquire_connector("10.0.0.1")
    b = _acquire_connector("10.0.0.1")

    assert a is b, "each entry opened its own pool"

    await _release_connector("10.0.0.1")
    await _release_connector("10.0.0.1")


async def test_different_hosts_get_their_own_pool():
    a = _acquire_connector("10.0.0.1")
    b = _acquire_connector("10.0.0.2")

    assert a is not b

    await _release_connector("10.0.0.1")
    await _release_connector("10.0.0.2")


async def test_releasing_one_entry_does_not_close_the_pool_for_the_others():
    """Unloading one channel must not break the other ten."""
    shared = _acquire_connector("10.0.0.1")
    _acquire_connector("10.0.0.1")
    _acquire_connector("10.0.0.1")

    await _release_connector("10.0.0.1")

    assert not shared.closed, "closed the pool while other entries were using it"
    assert _acquire_connector("10.0.0.1") is shared

    for _ in range(3):
        await _release_connector("10.0.0.1")


async def test_the_last_release_closes_the_pool():
    shared = _acquire_connector("10.0.0.1")
    _acquire_connector("10.0.0.1")

    await _release_connector("10.0.0.1")
    await _release_connector("10.0.0.1")

    assert shared.closed, "the pool leaked once nothing was using it"
    assert "10.0.0.1" not in dahua_module._HOST_CONNECTORS


async def test_a_new_pool_is_built_after_the_last_one_closed():
    first = _acquire_connector("10.0.0.1")
    await _release_connector("10.0.0.1")
    assert first.closed

    second = _acquire_connector("10.0.0.1")
    assert second is not first
    assert not second.closed
    await _release_connector("10.0.0.1")


async def test_releasing_more_than_acquired_is_harmless():
    """A double unload must not raise or corrupt the registry."""
    _acquire_connector("10.0.0.1")
    await _release_connector("10.0.0.1")
    await _release_connector("10.0.0.1")
    await _release_connector("10.0.0.2")

    assert dahua_module._HOST_CONNECTORS == {}
