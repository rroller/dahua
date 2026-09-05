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
