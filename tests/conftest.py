"""Configure pytest for dahua integration tests."""
import asyncio

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
def _clear_channel_numbering():
    """Whether a device numbers from zero is decided once and cached per device.

    That is the point of it, and it means one test's answer would otherwise be
    inherited by the next. The locks go too, because a lock belongs to the loop
    it was created on.
    """
    from custom_components import dahua as dahua_module

    dahua_module._HOST_CHANNEL_BASE.clear()
    dahua_module._HOST_CHANNEL_BASE_LOCKS.clear()
    yield
    dahua_module._HOST_CHANNEL_BASE.clear()
    dahua_module._HOST_CHANNEL_BASE_LOCKS.clear()


@pytest.fixture(autouse=True)
async def _clear_shared_rpc2():
    """The RPC2 registry holds a login task and a keepalive task.

    Both belong to the event loop of the test that made them, so leaving one
    behind hands the next test a task it cannot await -- the same reason the
    shared read cache is cleared above.

    Cancelling is not enough on its own. cancel() only schedules the
    cancellation, so the task stays pending until the loop gets a turn, and
    Home Assistant's own teardown then fails the test for leaving a lingering
    task. Hence awaiting them, and hence this fixture being async.
    """
    from custom_components.dahua import client as client_module

    async def _drain():
        cancelled = []
        for holder in list(client_module._HOST_RPC2.values()):
            if holder.keepalive is not None:
                holder.keepalive.cancel()
                cancelled.append(holder.keepalive)
            session = getattr(holder, "session", None)
            if session is not None and not session.closed:
                session.connector.close()
        client_module._HOST_RPC2.clear()
        client_module._HOST_RPC2_UNAVAILABLE.clear()
        # Which (host, table) pairs a device has refused is module state too, and
        # it was the one set here that nothing cleared: a test that made a device
        # refuse a table taught every later test the same thing, so a later read
        # of it skipped RPC2 for a reason that test never set up.
        client_module._RPC2_TABLE_UNAVAILABLE.clear()
        if cancelled:
            await asyncio.gather(*cancelled, return_exceptions=True)

    await _drain()
    yield
    await _drain()


@pytest.fixture(autouse=True)
async def _stop_shared_event_streams():
    """One event stream per host, each owning a task that outlives its test.

    Same reasoning as the RPC2 keepalive above: the task has to be awaited
    after cancelling, not merely cancelled, or the test fails in teardown for
    leaving it pending.
    """
    from custom_components import dahua as dahua_module

    async def _drain():
        cancelled = []
        for stream in list(dahua_module._HOST_STREAMS.values()):
            task = getattr(stream, "_task", None)
            if task is not None and not task.done():
                task.cancel()
                cancelled.append(task)
        dahua_module._HOST_STREAMS.clear()
        if cancelled:
            await asyncio.gather(*cancelled, return_exceptions=True)

    await _drain()
    yield
    await _drain()

@pytest.fixture(autouse=True)
def _clear_rpc2_event_state():
    """What the RPC2 poll last reported active is module state, per host.

    It has to survive a poller restart -- that is the point of it -- so it also
    survives a test, and one test's active event would otherwise be inherited as
    another's stale Start.
    """
    from custom_components import dahua as dahua_module
    from custom_components.dahua import client as client_module

    def _drain():
        client_module._HOST_RPC2_EVENT_STATE.clear()
        # The poller's description of itself, which diagnostics reads. Same reasoning:
        # it is keyed by host and outlives a restart, so it outlives a test too.
        client_module._HOST_RPC2_EVENT_POLL.clear()
        # And which channels have already had a capability refusal reported, or one
        # test's refusal silences the warning another test is asserting on.
        dahua_module._CAPABILITY_REFUSALS_REPORTED.clear()

    _drain()
    yield
    _drain()

@pytest.fixture(autouse=True)
def _clear_cgi_config_absent():
    """Which hosts have no CGI config endpoint is learnt once, per host.

    It is module state so one 404 teaches every later read, which also means one
    test would otherwise teach the next -- and a test that expected a CGI call
    would silently get an RPC2 one.
    """
    from custom_components.dahua import client as client_module

    client_module._HOST_CGI_CONFIG_ABSENT.clear()
    yield
    client_module._HOST_CGI_CONFIG_ABSENT.clear()
