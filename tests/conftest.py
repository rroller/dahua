"""Configure pytest for dahua integration tests."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import ClientError, ClientResponseError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.dahua.client import DahuaClient
from custom_components.dahua.const import (
    CONF_ADDRESS,
    CONF_CHANNEL,
    CONF_EVENTS,
    CONF_NAME,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_RTSP_PORT,
    CONF_USERNAME,
    DOMAIN,
)

# Re-export fixtures from pytest-homeassistant-custom-component
pytest_plugins = ["pytest_homeassistant_custom_component"]


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable custom integrations so HA's loader can find custom_components/dahua."""
    yield


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    """Return a MockConfigEntry for dahua."""
    return MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_USERNAME: "admin",
            CONF_PASSWORD: "password",
            CONF_ADDRESS: "192.168.1.108",
            CONF_PORT: "80",
            CONF_RTSP_PORT: "554",
            CONF_CHANNEL: 0,
            CONF_EVENTS: ["VideoMotion", "CrossLineDetection"],
            CONF_NAME: "TestCam",
        },
        title="TestCam",
        unique_id="SERIAL123",
    )


@pytest.fixture
def mock_client() -> AsyncMock:
    """Return an AsyncMock of DahuaClient with sensible defaults."""
    client = AsyncMock(spec=DahuaClient)

    # Init sequence returns
    client.get_max_extra_streams.return_value = 2
    client.async_get_machine_name.return_value = {
        "table.General.MachineName": "TestCam"
    }
    client.async_get_system_info.return_value = {
        "serialNumber": "SERIAL123",
        "deviceType": "IPC-HDW5831R-ZE",
    }
    client.get_software_version.return_value = {
        "version": "2.800.0000016.0.R,build:2020-06-05"
    }
    client.get_device_type.return_value = {"type": "IPC-HDW5831R-ZE"}
    client.async_get_snapshot.side_effect = ClientError("not supported")

    # Capability detection: raise by default so tests opt-in
    req_info = MagicMock()
    req_info.real_url = "http://test"

    client.async_get_coaxial_control_io_status.side_effect = ClientResponseError(
        req_info, ()
    )
    client.async_get_disarming_linkage.side_effect = ClientError()
    client.async_get_event_notifications.side_effect = ClientError()
    client.async_get_ptz_position.side_effect = ClientError()
    client.async_get_smart_motion_detection.side_effect = ClientError()
    client.async_get_config_lighting.side_effect = ClientError()
    client.async_get_lighting_v2.side_effect = ClientError()
    client.async_get_config.side_effect = ClientError()

    # Periodic polling defaults
    client.async_get_config_motion_detection.return_value = {
        "table.MotionDetect[0].Enable": "true"
    }

    # Static/simple methods - use real implementations
    client.to_stream_name = DahuaClient.to_stream_name
    client.get_rtsp_stream_url.side_effect = (
        lambda channel, subtype: f"rtsp://admin:password@192.168.1.108:554/cam/realmonitor?channel={channel}&subtype={subtype}"
    )

    return client


@pytest.fixture
def mock_coordinator(hass, mock_config_entry, mock_client):
    """Return a DahuaDataUpdateCoordinator with mock client, already initialized."""
    from custom_components.dahua import DahuaDataUpdateCoordinator

    with patch.object(
        DahuaDataUpdateCoordinator, "__init__", lambda self, *a, **kw: None
    ):
        coordinator = DahuaDataUpdateCoordinator.__new__(DahuaDataUpdateCoordinator)

    # Set up all the attributes that __init__ normally sets
    coordinator.hass = hass
    coordinator.client = mock_client
    coordinator.config_entry = mock_config_entry
    coordinator.platforms = []
    coordinator.initialized = True
    coordinator.model = "IPC-HDW5831R-ZE"
    coordinator.machine_name = "TestCam"
    coordinator.connected = True
    coordinator.events = ["VideoMotion", "CrossLineDetection"]
    coordinator._supports_coaxial_control = False
    coordinator._supports_disarming_linkage = False
    coordinator._supports_event_notifications = False
    coordinator._supports_smart_motion_detection = False
    coordinator._supports_ptz_position = False
    coordinator._supports_lighting = True
    coordinator._supports_lighting_v2 = False
    coordinator._supports_floodlightmode = False
    coordinator._supports_profile_mode = False
    coordinator._serial_number = "SERIAL123"
    coordinator._profile_mode = "0"
    coordinator._preset_position = "0"
    coordinator._channel = 0
    coordinator._channel_number = 1
    coordinator._address = "192.168.1.108"
    coordinator._max_streams = 3
    coordinator._name = "TestCam"
    coordinator._username = "admin"
    coordinator._password = "password"
    coordinator._event_task = None
    coordinator._vto_task = None
    coordinator._vto_client = None
    coordinator._dahua_event_listeners = {}
    coordinator._dahua_event_timestamp = {}
    coordinator._floodlight_mode = 2
    coordinator._session = AsyncMock()
    coordinator.data = {}
    coordinator.logger = MagicMock()
    coordinator.name = DOMAIN
    coordinator.last_update_success = True

    return coordinator
