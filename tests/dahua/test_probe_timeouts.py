"""A capability probe that times out must not take the config entry down.

Every request is wrapped in asyncio.timeout(TIMEOUT_SECONDS), which raises the
builtin TimeoutError. That is not an aiohttp.ClientError, so a probe catching
only ClientError let the timeout escape, hit the outer handler and fail the
whole entry with ConfigEntryNotReady -- for a capability the device merely
happens to be slow about. Home Assistant then retried forever.

Reported as #631 (setup fails, 20s hang -- exactly TIMEOUT_SECONDS) and #594
(PTZ probe timing out for eight days straight, 700+ occurrences).
"""

from datetime import timedelta
from types import SimpleNamespace

import pytest
from aiohttp import ClientError

from custom_components import dahua as dahua_module
from custom_components.dahua import DahuaDataUpdateCoordinator


@pytest.fixture(autouse=True)
def _clean_host_failures():
    """Failures are tracked per host in module state, not per coordinator."""
    dahua_module._HOST_FAILURES.clear()
    yield
    dahua_module._HOST_FAILURES.clear()

PROBES = [
    ("async_probe_snapshot", None),
    ("async_get_coaxial_control_io_status", "_supports_coaxial_control"),
    ("async_get_disarming_linkage", "_supports_disarming_linkage"),
    ("async_get_event_notifications", "_supports_event_notifications"),
    ("async_get_ptz_position", "_supports_ptz_position"),
    ("async_get_smart_motion_detection", "_supports_smart_motion_detection"),
    ("async_get_lighting_v2", "_supports_lighting_v2"),
]

# What the device must answer before any probe runs. These are not probes: if
# they fail the entry genuinely cannot be set up.
REQUIRED = {
    "get_max_extra_streams": 2,
    "async_get_machine_name": {"table.General.MachineName": "Cam"},
    "async_get_system_info": {"deviceType": "IPC-HDW1234", "serialNumber": "SER1"},
    "get_software_version": {"version": "2.800.0"},
}


class _Client:
    """Answers everything, except the calls told to fail."""

    def __init__(self, failing=(), error=TimeoutError):
        self.failing = set(failing)
        self.error = error
        self.called = []

    def __getattr__(self, name):
        async def call(*args, **kwargs):
            self.called.append(name)
            if name in self.failing:
                raise self.error()
            if name in REQUIRED:
                return REQUIRED[name]
            return {}
        return call


async def _noop(*args, **kwargs):
    return None


def _coordinator(client):
    c = object.__new__(DahuaDataUpdateCoordinator)
    c.client = client
    c.hass = SimpleNamespace(async_create_task=lambda *a, **k: None)
    # Starting the event stream is not what these pin, and it needs a great
    # deal of unrelated state to reach.
    c.async_start_event_listener = _noop
    c.async_start_vto_event_listener = _noop
    c._address = "10.0.0.7"
    c._channel = 0
    c._channel_number = 1
    c.initialized = False
    c.model = ""
    c.machine_name = None
    c._serial_number = None
    c._max_streams = 1
    c._profile_mode = 0
    c._preset_position = "0"
    c._supports_profile_mode = False
    c._supports_ptz_position = False
    c._supports_disarming_linkage = False
    c._supports_event_notifications = False
    c._supports_coaxial_control = False
    c._supports_smart_motion_detection = False
    c._supports_lighting = False
    c._supports_lighting_v2 = False
    c._supports_floodlightmode = False
    c.config_entry = SimpleNamespace(data={}, options={}, entry_id="e1")
    c.update_interval = timedelta(seconds=30)
    return c


@pytest.mark.parametrize("method,flag", PROBES)
async def test_a_probe_that_times_out_does_not_fail_setup(method, flag):
    """This is the bug: one slow capability check took the whole device down."""
    coordinator = _coordinator(_Client(failing=[method], error=TimeoutError))

    await coordinator._async_update_data()

    assert coordinator.initialized, f"{method} timing out failed the whole entry"
    if flag:
        assert getattr(coordinator, flag) is False, f"{flag} should be off after a timeout"


@pytest.mark.parametrize("method,flag", PROBES)
async def test_a_probe_that_errors_still_marks_it_unsupported(method, flag):
    """The existing behaviour must not change."""
    coordinator = _coordinator(_Client(failing=[method], error=ClientError))

    await coordinator._async_update_data()

    assert coordinator.initialized
    if flag:
        assert getattr(coordinator, flag) is False


async def test_every_probe_timing_out_at_once_still_sets_up():
    """A slow device fails every probe, and must still produce a usable entry."""
    coordinator = _coordinator(_Client(failing=[m for m, _ in PROBES], error=TimeoutError))

    await coordinator._async_update_data()

    assert coordinator.initialized


async def test_a_required_call_timing_out_still_fails_setup():
    """Not everything is a probe: without the system info there is no device."""
    coordinator = _coordinator(_Client(failing=["async_get_system_info"], error=TimeoutError))

    with pytest.raises(Exception):
        await coordinator._async_update_data()

    assert not coordinator.initialized
