"""Tests for binary_sensor platform."""

import pytest

from custom_components.dahua.binary_sensor import (
    DahuaEventSensor,
    async_setup_entry,
    DEVICE_CLASS_OVERRIDES,
    ICON_OVERRIDES,
    NAME_OVERRIDES,
)


class TestAsyncSetupEntry:
    @pytest.mark.asyncio
    async def test_creates_sensors_for_events(
        self, mock_coordinator, mock_config_entry
    ):
        """Creates a binary sensor for each configured event."""
        added = []
        mock_config_entry.runtime_data = mock_coordinator

        await async_setup_entry(None, mock_config_entry, added.append)

        names = [s.name for sensors in added for s in sensors]
        assert "Motion Alarm" in names
        assert "Cross Line Alarm" in names

    @pytest.mark.asyncio
    async def test_doorbell_extras(self, mock_coordinator, mock_config_entry):
        """Doorbells get extra sensors (DoorbellPressed, Invite, DoorStatus, CallNoAnswered)."""
        mock_coordinator.model = "VTO2101E-P"
        mock_config_entry.runtime_data = mock_coordinator
        added = []

        await async_setup_entry(None, mock_config_entry, added.append)

        all_sensors = [s for sensors in added for s in sensors]
        event_names = [s._event_name for s in all_sensors]
        assert "DoorbellPressed" in event_names
        assert "Invite" in event_names
        assert "DoorStatus" in event_names
        assert "CallNoAnswered" in event_names


class TestDahuaEventSensor:
    def test_name_override(self, mock_coordinator, mock_config_entry):
        sensor = DahuaEventSensor(mock_coordinator, mock_config_entry, "VideoMotion")
        assert sensor.name == "Motion Alarm"

    def test_name_camel_case_split(self, mock_coordinator, mock_config_entry):
        sensor = DahuaEventSensor(
            mock_coordinator, mock_config_entry, "SmartMotionHuman"
        )
        assert sensor.name == "Smart Motion Human"

    def test_unique_id_video_motion(self, mock_coordinator, mock_config_entry):
        """VideoMotion uses just serial number for backwards compatibility."""
        sensor = DahuaEventSensor(mock_coordinator, mock_config_entry, "VideoMotion")
        assert sensor.unique_id == "SERIAL123"

    def test_unique_id_other_event(self, mock_coordinator, mock_config_entry):
        sensor = DahuaEventSensor(
            mock_coordinator, mock_config_entry, "CrossLineDetection"
        )
        assert sensor.unique_id == "SERIAL123_cross_line_alarm"

    def test_device_class_override(self, mock_coordinator, mock_config_entry):
        sensor = DahuaEventSensor(mock_coordinator, mock_config_entry, "VideoMotion")
        assert sensor.device_class == "motion"

    def test_device_class_safety(self, mock_coordinator, mock_config_entry):
        sensor = DahuaEventSensor(mock_coordinator, mock_config_entry, "AlarmLocal")
        assert sensor.device_class == "safety"

    def test_icon_override(self, mock_coordinator, mock_config_entry):
        sensor = DahuaEventSensor(mock_coordinator, mock_config_entry, "AudioAnomaly")
        assert sensor.icon == "mdi:volume-high"

    def test_icon_none_for_normal(self, mock_coordinator, mock_config_entry):
        sensor = DahuaEventSensor(mock_coordinator, mock_config_entry, "VideoMotion")
        assert sensor.icon is None

    def test_is_on_true(self, mock_coordinator, mock_config_entry):
        mock_coordinator._dahua_event_timestamp["VideoMotion-0"] = 1000
        sensor = DahuaEventSensor(mock_coordinator, mock_config_entry, "VideoMotion")
        assert sensor.is_on is True

    def test_is_on_false(self, mock_coordinator, mock_config_entry):
        mock_coordinator._dahua_event_timestamp["VideoMotion-0"] = 0
        sensor = DahuaEventSensor(mock_coordinator, mock_config_entry, "VideoMotion")
        assert sensor.is_on is False

    @pytest.mark.asyncio
    async def test_async_added_to_hass_registers_listener(
        self, mock_coordinator, mock_config_entry
    ):
        sensor = DahuaEventSensor(mock_coordinator, mock_config_entry, "VideoMotion")
        await sensor.async_added_to_hass()
        assert "VideoMotion-0" in mock_coordinator._dahua_event_listeners

    def test_should_poll_false(self, mock_coordinator, mock_config_entry):
        sensor = DahuaEventSensor(mock_coordinator, mock_config_entry, "VideoMotion")
        assert sensor.should_poll is False
