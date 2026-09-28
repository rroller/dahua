"""Tests for doorbell detection and ANPR stream event handling."""
from unittest.mock import MagicMock
import pytest

from custom_components.dahua import DahuaDataUpdateCoordinator
from custom_components.dahua.const import (
    CONF_AUTHORIZED_PLATES,
    EVENT_DAHUA_ANPR_RECOGNIZED,
)


def _coordinator(model: str) -> DahuaDataUpdateCoordinator:
    coordinator = object.__new__(DahuaDataUpdateCoordinator)
    coordinator.model = model
    return coordinator


class TestIsDoorbell:
    """Tests for is_doorbell model matching."""

    @pytest.mark.parametrize(
        "model",
        [
            "DHI-ITC413-PW4D-IZ1",     # ANPR camera (previously misidentified due to DHI prefix)
            "DHI-IPC-HFW5241E-Z12E",   # Standard Dahua IPC camera
            "DHI-IPC-HDW5442TM-AS",    # Eyeball camera
            "DHI-NVR5216-16P-I",       # NVR
            "IPC-HFW4431R-Z",          # Dahua bullet without DHI prefix
            "SD49225T-HN",             # PTZ camera
            "NVR4208-8P-4KS2",         # NVR without DHI prefix
        ],
    )
    def test_non_doorbells_return_false(self, model: str):
        assert not _coordinator(model).is_doorbell()

    @pytest.mark.parametrize(
        "model",
        [
            "VTO2111D-WP",
            "DH-VTO2000A",
            "DHI-VTO2211G-WP-S2",
            "DHI-VTO2202F-P",
            "DH_VTO1210B-X",
            "DHI_VTO6210B",
            "OEM-VTO2000A",
            "CUSTOM_VTO100",
            "AD410",
            "AD110",
            "DB61i",
            "DB2X",
            "AV-VT01",
        ],
    )
    def test_doorbells_return_true(self, model: str):
        assert _coordinator(model).is_doorbell()


class TestAnprPlateHandlingAcrossStreams:
    """Tests for ANPR plate extraction across event streams."""

    def _setup_coordinator(self, authorized_plates="ABC1234, XYZ5678") -> DahuaDataUpdateCoordinator:
        coordinator = object.__new__(DahuaDataUpdateCoordinator)
        coordinator._name = "LPR Camera"
        coordinator._address = "10.0.0.1"
        coordinator._channel = 0
        coordinator.model = "DHI-ITC413-PW4D-IZ1"
        coordinator._last_plate_data = {}
        coordinator._last_plate_timestamp = 0
        coordinator._plate_listeners = []
        coordinator._dahua_event_listeners = {}
        coordinator._dahua_event_timestamp = {}
        coordinator.events = []

        coordinator.config_entry = MagicMock()
        coordinator.config_entry.options = {CONF_AUTHORIZED_PLATES: authorized_plates}
        coordinator.config_entry.data = {}

        coordinator.hass = MagicMock()
        return coordinator

    def test_handle_anpr_plate_authorized_vehicle(self):
        coordinator = self._setup_coordinator()
        listener_called = []
        coordinator.add_plate_listener(lambda: listener_called.append(True))

        event = {
            "Code": "TrafficJunction",
            "action": "Pulse",
            "index": "0",
            "data": {
                "Object": {
                    "ObjectType": "Plate",
                    "Text": "ABC-1234",
                    "Confidence": 96,
                },
                "TrafficCar": {
                    "PlateNumber": "ABC-1234",
                    "VehicleType": "SUV",
                    "PlateColor": "White",
                },
            },
        }

        coordinator._handle_anpr_plate(event)

        assert coordinator._last_plate_data["plate"] == "ABC1234"
        assert event["PlateNumber"] == "ABC1234"
        assert event["PlateData"] == coordinator._last_plate_data
        assert len(listener_called) == 1

        coordinator.hass.bus.fire.assert_called_once()
        call_args = coordinator.hass.bus.fire.call_args
        assert call_args[0][0] == EVENT_DAHUA_ANPR_RECOGNIZED
        event_data = call_args[0][1]
        assert event_data["plate"] == "ABC1234"
        assert event_data["is_authorized"] is True
        assert event_data["confidence"] == 96
        assert event_data["vehicle_type"] == "SUV"

    def test_handle_anpr_plate_unauthorized_vehicle(self):
        coordinator = self._setup_coordinator()
        event = {
            "Code": "TrafficJunction",
            "action": "Pulse",
            "index": "0",
            "data": {
                "TrafficCar": {
                    "PlateNumber": "UNKNOWN999",
                }
            },
        }

        coordinator._handle_anpr_plate(event)

        assert coordinator._last_plate_data["plate"] == "UNKNOWN999"
        coordinator.hass.bus.fire.assert_called_once()
        event_data = coordinator.hass.bus.fire.call_args[0][1]
        assert event_data["plate"] == "UNKNOWN999"
        assert event_data["is_authorized"] is False

    def test_on_receive_vto_event_processes_anpr_plate(self):
        coordinator = self._setup_coordinator()
        event = {
            "Code": "TrafficJunction",
            "action": "Pulse",
            "index": "0",
            "data": {
                "TrafficCar": {
                    "PlateNumber": "XYZ-5678",
                }
            },
        }

        coordinator.on_receive_vto_event(event)

        assert coordinator._last_plate_data["plate"] == "XYZ5678"
        fired_events = [call[0][0] for call in coordinator.hass.bus.fire.call_args_list]
        assert EVENT_DAHUA_ANPR_RECOGNIZED in fired_events
        assert "dahua_event_received" in fired_events

    def test_handle_event_processes_anpr_plate(self):
        coordinator = self._setup_coordinator()
        coordinator.is_connected = MagicMock(return_value=True)

        event = {
            "Code": "TrafficJunction",
            "action": "Pulse",
            "index": "0",
            "data": {
                "TrafficCar": {
                    "PlateNumber": "ABC1234",
                }
            },
        }

        coordinator.handle_event(event)

        assert coordinator._last_plate_data["plate"] == "ABC1234"
        fired_events = [call[0][0] for call in coordinator.hass.bus.fire.call_args_list]
        assert EVENT_DAHUA_ANPR_RECOGNIZED in fired_events
        assert "dahua_event_received" in fired_events
