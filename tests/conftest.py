"""Configure pytest for dahua integration tests."""
import pytest

# Re-export fixtures from pytest-homeassistant-custom-component
pytest_plugins = ["pytest_homeassistant_custom_component"]


@pytest.fixture(autouse=True)
def _clear_shared_host_reads():
    """The host read cache is module state and holds tasks.

    A task left behind by one test belongs to that test's event loop, so this
    has to be cleared for every test, not just the ones that know about it.
    """
    from custom_components.dahua import client as client_module

    client_module._HOST_CACHE.clear()
    yield
    client_module._HOST_CACHE.clear()


@pytest.fixture(autouse=True)
def _clear_shared_rpc2():
    """The RPC2 registry holds a login task and a keepalive task.

    Both belong to the event loop of the test that made them, so leaving one
    behind hands the next test a task it cannot await -- the same reason the
    shared read cache is cleared above.
    """
    from custom_components.dahua import client as client_module

    client_module._HOST_RPC2.clear()
    client_module._HOST_RPC2_UNAVAILABLE.clear()
    yield
    client_module._HOST_RPC2.clear()
    client_module._HOST_RPC2_UNAVAILABLE.clear()
