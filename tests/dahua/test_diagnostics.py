"""A diagnostics dump gets pasted into public issue threads."""

import json
import re
from types import SimpleNamespace

import pytest
from homeassistant.helpers.json import ExtendedJSONEncoder
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components import dahua as dahua_module
from custom_components.dahua import client as client_module
from custom_components.dahua.const import DOMAIN
from custom_components.dahua.diagnostics import (
    async_get_config_entry_diagnostics,
    async_get_device_diagnostics,
)

# Distinctive so a substring search over the whole blob is meaningful.
PASSWORD = "hunter2-must-never-appear"
USERNAME = "admin-unique-token"
SERIAL = "4X7C5A1ZAG21L3F"
ADDRESS = "10.0.0.1"


@pytest.fixture(autouse=True)
def _clean_registries():
    dahua_module._HOST_CONNECTORS.clear()
    client_module._HOST_LIMITS.clear()
    yield
    dahua_module._HOST_CONNECTORS.clear()
    client_module._HOST_LIMITS.clear()


class _Client:
    def __init__(self):
        self._base = f"http://{ADDRESS}:80"
        self._address = ADDRESS
        self._port = 80
        self._rtsp_port = 554
        self._use_https = None
        self._digest_state = {}
        self._rpc2_session_instance = None
        self._host_limit = client_module._host_limiter(ADDRESS)
        self.identity_derived_from_credentials = False

    def get_rtsp_stream_url(self, channel, subtype):
        # Present because the real client has it; the dump must never call it.
        return f"rtsp://{USERNAME}:{PASSWORD}@{ADDRESS}:554/cam/realmonitor"


_UNSET = object()


class _Coordinator:
    def __init__(self, *, data=_UNSET, initialized=True):
        self.client = _Client()
        # A sentinel, so a test can express "data is None" - the state a
        # coordinator is in before its first successful refresh.
        self.data = {"table.Foo": "1"} if data is _UNSET else data
        self.initialized = initialized
        self.last_update_success = True
        self.last_update_success_time = None
        self.last_exception = None
        self.update_interval = None
        self.platforms = ["camera"]
        self.machine_name = "FrontDoorCam"
        self._supports_coaxial_control = True
        self._supports_disarming_linkage = False
        self._supports_lighting_v2 = True
        self._dahua_event_timestamp = {"VideoMotion-0": 0}
        self._dahua_event_listeners = {"VideoMotion-0": object()}
        self._event_task = None
        self._vto_task = None
        self._vto_client = None

    def get_model(self):
        return "IPC-HDW5831R-ZE"

    def get_device_name(self):
        return "Front Door"

    def get_firmware_version(self):
        return "2.800.0000016.0.R"

    def get_serial_number(self):
        return SERIAL

    def get_channel(self):
        return 0

    def get_channel_number(self):
        return 1

    def get_max_streams(self):
        return 3

    def get_profile_mode(self):
        return "0"

    def get_event_list(self):
        return ["VideoMotion"]

    def is_doorbell(self):
        return False

    def is_amcrest_doorbell(self):
        return False

    def is_flood_light(self):
        return False

    def supports_siren(self):
        return False

    def supports_security_light(self):
        return False

    def supports_infrared_light(self):
        return True

    def supports_illuminator(self):
        # Deliberately dereferences data, like the real one
        return self.data["table.Lighting"] == "1"

    def supports_smart_motion_detection_amcrest(self):
        return False


def _entry(hass, *, address=ADDRESS, channel=0, title="Front Door"):
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=title,
        unique_id=SERIAL if channel == 0 else f"{SERIAL}_{channel}",
        data={
            "username": USERNAME,
            "password": PASSWORD,
            "address": address,
            "port": "80",
            "rtsp_port": "554",
            "channel": channel,
            "name": title,
        },
        options={"events": ["VideoMotion"], "scan_interval": 120},
    )
    entry.add_to_hass(hass)
    return entry


def _install(hass, entry, coordinator=None):
    coordinator = coordinator or _Coordinator()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    return coordinator


async def _blob(hass, entry):
    return json.dumps(
        await async_get_config_entry_diagnostics(hass, entry), cls=ExtendedJSONEncoder
    )


# --- the things that must never leak ---------------------------------------

async def test_no_credential_appears_anywhere_in_the_dump(hass):
    """Asserted over the whole blob, not per key.

    Checking result["entry"]["data"]["password"] == REDACTED passes happily
    while the password leaks through some other key, which is exactly what an
    RTSP URL would do - async_redact_data redacts by key, not by value.
    """
    entry = _entry(hass)
    _install(hass, entry)

    blob = await _blob(hass, entry)

    assert PASSWORD not in blob
    assert USERNAME not in blob


async def test_the_serial_number_is_never_published(hass):
    entry = _entry(hass)
    _install(hass, entry)

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert SERIAL not in json.dumps(result, cls=ExtendedJSONEncoder)
    assert re.fullmatch(r"[0-9a-f]{12}", result["device"]["serial_fingerprint"])


async def test_the_same_device_fingerprints_the_same_across_entries(hass):
    """The fingerprint has to still group an NVR's channels together."""
    a, b = _entry(hass, channel=0), _entry(hass, channel=1, title="Ch2")
    _install(hass, a)
    _install(hass, b)

    fa = (await async_get_config_entry_diagnostics(hass, a))["device"]["serial_fingerprint"]
    fb = (await async_get_config_entry_diagnostics(hass, b))["device"]["serial_fingerprint"]

    assert fa == fb


async def test_the_digest_state_is_reported_only_as_a_boolean(hass):
    entry = _entry(hass)
    coordinator = _install(hass, entry)
    coordinator.client._digest_state = {"nonce": "NONCE-SENTINEL-VALUE"}

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert "NONCE-SENTINEL-VALUE" not in json.dumps(result, cls=ExtendedJSONEncoder)
    assert result["client"]["digest_challenge_cached"] is True


# --- it must not fall over, especially when things are broken --------------

async def test_the_dump_is_json_serialisable(hass):
    """A non-serialisable field is a silent HTTP 500 with only a log line."""
    entry = _entry(hass)
    _install(hass, entry)

    json.dumps(await async_get_config_entry_diagnostics(hass, entry),
               cls=ExtendedJSONEncoder)


async def test_the_dump_survives_a_coordinator_that_never_refreshed(hass):
    """The state a dump is most needed in: setup failed, data is None."""
    entry = _entry(hass)
    coordinator = _Coordinator(data=None, initialized=False)
    coordinator.last_update_success = False
    coordinator.last_exception = TimeoutError("no route to host")
    _install(hass, entry, coordinator)

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["coordinator"]["last_exception_type"] == "TimeoutError"
    assert result["coordinator"]["data_key_count"] == 0
    # supports_illuminator dereferences data and would raise
    assert result["capabilities"]["derived_from_model"]["supports_illuminator"] is None


async def test_the_dump_survives_a_missing_serial_number(hass):
    """get_serial_number reads an attribute assigned only during detection."""
    entry = _entry(hass)
    coordinator = _install(hass, entry)
    coordinator.get_serial_number = lambda: (_ for _ in ()).throw(AttributeError())

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["device"]["serial_fingerprint"] is None


# --- the content that makes it worth pulling -------------------------------

async def test_effective_options_are_reported_not_just_stored_ones(hass):
    entry = _entry(hass)
    _install(hass, entry)

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["entry"]["resolved_events"] == ["VideoMotion"]
    assert result["entry"]["resolved_scan_interval_seconds"] == 120


async def test_every_capability_flag_is_represented(hass):
    """A flag added later should show up here without anyone remembering to."""
    entry = _entry(hass)
    coordinator = _install(hass, entry)

    result = await async_get_config_entry_diagnostics(hass, entry)

    expected = {n for n in vars(coordinator) if n.startswith("_supports_")}
    assert len(result["capabilities"]["probed"]) == len(expected)
    assert result["capabilities"]["probed"]["coaxial_control"] is True
    assert result["capabilities"]["probed"]["disarming_linkage"] is False


async def test_event_timestamps_are_reported_as_ages(hass):
    """A six-hour-old motion event is a sensor stuck on with no Stop."""
    import time

    entry = _entry(hass)
    coordinator = _install(hass, entry)
    coordinator._dahua_event_timestamp = {
        "VideoMotion-0": int(time.time()) - 21600,
        "CrossLineDetection-0": 0,
    }

    events = (await async_get_config_entry_diagnostics(hass, entry))["events"]

    assert 21590 <= events["timestamp_age_seconds"]["VideoMotion-0"] <= 21610
    assert events["timestamp_age_seconds"]["CrossLineDetection-0"] is None
    assert events["active_count"] == 1


async def test_siblings_on_one_address_are_reported(hass):
    """The point of the host block: this report is 1 of N against one NVR."""
    entries = [_entry(hass, channel=i, title=f"Ch{i}") for i in range(3)]
    other = _entry(hass, address="10.0.0.2", channel=0, title="Doorbell")
    for entry in [*entries, other]:
        _install(hass, entry)

    host = (await async_get_config_entry_diagnostics(hass, entries[0]))["host"]

    assert host["sibling_entry_count"] == 3
    assert host["total_dahua_entries"] == 4
    assert sum(1 for s in host["sibling_entries"] if s["is_this_entry"]) == 1
    assert other.entry_id not in [s["entry_id"] for s in host["sibling_entries"]]


async def test_the_shared_connector_refcount_is_reported(hass):
    entry = _entry(hass)
    _install(hass, entry)
    dahua_module._acquire_connector(ADDRESS)
    dahua_module._acquire_connector(ADDRESS)

    host = (await async_get_config_entry_diagnostics(hass, entry))["host"]

    assert host["connector_refcount"] == 2
    assert host["connector_closed"] is False
    await dahua_module._release_connector(ADDRESS)
    await dahua_module._release_connector(ADDRESS)


async def test_the_per_host_limiter_is_reported(hass):
    entry = _entry(hass)
    _install(hass, entry)

    host = (await async_get_config_entry_diagnostics(hass, entry))["host"]

    assert host["max_concurrent_requests_per_host"] == (
        client_module.MAX_CONCURRENT_REQUESTS_PER_HOST
    )
    assert host["limiter_free_slots"] == client_module.MAX_CONCURRENT_REQUESTS_PER_HOST
    assert host["limiter_locked"] is False


async def test_a_mismatched_host_key_is_visible(hass):
    """One device holding two differently-keyed pools should be obvious."""
    entry = _entry(hass, address=f"{ADDRESS}/")
    _install(hass, entry)

    host = (await async_get_config_entry_diagnostics(hass, entry))["host"]

    assert host["client_key_matches_connector_key"] is False


# --- device diagnostics ----------------------------------------------------

async def test_device_diagnostics_does_not_publish_the_identifiers(hass):
    entry = _entry(hass)
    _install(hass, entry)
    device = SimpleNamespace(
        identifiers={(DOMAIN, SERIAL)}, name="Front Door", name_by_user=None,
        model="IPC", manufacturer="Dahua", sw_version="1.0", area_id=None,
        disabled_by=None, entry_type=None,
    )

    result = await async_get_device_diagnostics(hass, entry, device)

    assert SERIAL not in json.dumps(result, cls=ExtendedJSONEncoder)
    assert result["device_registry"]["name"] == "Front Door"
    assert "coordinator" in result
