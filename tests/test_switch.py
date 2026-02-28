"""Tests for switch platform."""

from unittest.mock import AsyncMock, patch

import aiohttp
import pytest

from custom_components.dahua.switch import (
    DahuaDisarmingEventNotificationsLinkageBinarySwitch,
    DahuaDisarmingLinkageBinarySwitch,
    DahuaMotionDetectionBinarySwitch,
    DahuaSirenBinarySwitch,
    DahuaSmartMotionDetectionBinarySwitch,
    async_setup_entry,
)


class TestAsyncSetupEntry:
    @pytest.mark.asyncio
    async def test_basic_setup(self, mock_coordinator, mock_config_entry):
        """Always adds motion detection switch."""
        mock_config_entry.runtime_data = mock_coordinator
        added = []

        await async_setup_entry(None, mock_config_entry, added.append)

        types = [type(s).__name__ for sensors in added for s in sensors]
        assert "DahuaMotionDetectionBinarySwitch" in types

    @pytest.mark.asyncio
    async def test_siren_switch(self, mock_coordinator, mock_config_entry):
        """Siren switch added for AS-PV models."""
        mock_coordinator.model = "IPC-HDW3849HP-AS-PV"
        mock_config_entry.runtime_data = mock_coordinator
        added = []

        await async_setup_entry(None, mock_config_entry, added.append)

        types = [type(s).__name__ for sensors in added for s in sensors]
        assert "DahuaSirenBinarySwitch" in types

    @pytest.mark.asyncio
    async def test_smart_motion_switch(self, mock_coordinator, mock_config_entry):
        """Smart motion switch added when supported."""
        mock_coordinator._supports_smart_motion_detection = True
        mock_config_entry.runtime_data = mock_coordinator
        added = []

        await async_setup_entry(None, mock_config_entry, added.append)

        types = [type(s).__name__ for sensors in added for s in sensors]
        assert "DahuaSmartMotionDetectionBinarySwitch" in types

    @pytest.mark.asyncio
    async def test_disarming_switches(self, mock_coordinator, mock_config_entry):
        """Disarming switches added when disarming linkage API works."""
        mock_coordinator.client.async_get_disarming_linkage = AsyncMock(return_value={})
        mock_config_entry.runtime_data = mock_coordinator
        added = []

        await async_setup_entry(None, mock_config_entry, added.append)

        types = [type(s).__name__ for sensors in added for s in sensors]
        assert "DahuaDisarmingLinkageBinarySwitch" in types
        assert "DahuaDisarmingEventNotificationsLinkageBinarySwitch" in types

    @pytest.mark.asyncio
    async def test_disarming_not_supported(self, mock_coordinator, mock_config_entry):
        """Disarming switches not added when API fails."""
        mock_coordinator.client.async_get_disarming_linkage = AsyncMock(
            side_effect=aiohttp.ClientError()
        )
        mock_config_entry.runtime_data = mock_coordinator
        added = []

        await async_setup_entry(None, mock_config_entry, added.append)

        types = [type(s).__name__ for sensors in added for s in sensors]
        assert "DahuaDisarmingLinkageBinarySwitch" not in types


class TestMotionDetectionSwitch:
    def test_unique_id(self, mock_coordinator, mock_config_entry):
        sw = DahuaMotionDetectionBinarySwitch(mock_coordinator, mock_config_entry)
        assert sw.unique_id == "SERIAL123_motion_detection"

    def test_name(self, mock_coordinator, mock_config_entry):
        sw = DahuaMotionDetectionBinarySwitch(mock_coordinator, mock_config_entry)
        assert sw.name == "Motion Detection"

    def test_icon(self, mock_coordinator, mock_config_entry):
        sw = DahuaMotionDetectionBinarySwitch(mock_coordinator, mock_config_entry)
        assert sw.icon == "mdi:motion-sensor"

    def test_is_on(self, mock_coordinator, mock_config_entry):
        mock_coordinator.data = {"table.MotionDetect[0].Enable": "true"}
        sw = DahuaMotionDetectionBinarySwitch(mock_coordinator, mock_config_entry)
        assert sw.is_on is True

    @pytest.mark.asyncio
    async def test_turn_on(self, mock_coordinator, mock_config_entry):
        mock_coordinator.client.enable_motion_detection = AsyncMock()
        mock_coordinator.async_refresh = AsyncMock()
        sw = DahuaMotionDetectionBinarySwitch(mock_coordinator, mock_config_entry)

        await sw.async_turn_on()
        mock_coordinator.client.enable_motion_detection.assert_called_once_with(0, True)

    @pytest.mark.asyncio
    async def test_turn_off(self, mock_coordinator, mock_config_entry):
        mock_coordinator.client.enable_motion_detection = AsyncMock()
        mock_coordinator.async_refresh = AsyncMock()
        sw = DahuaMotionDetectionBinarySwitch(mock_coordinator, mock_config_entry)

        await sw.async_turn_off()
        mock_coordinator.client.enable_motion_detection.assert_called_once_with(
            0, False
        )


class TestSirenSwitch:
    def test_unique_id(self, mock_coordinator, mock_config_entry):
        sw = DahuaSirenBinarySwitch(mock_coordinator, mock_config_entry)
        assert sw.unique_id == "SERIAL123_siren"

    def test_name(self, mock_coordinator, mock_config_entry):
        sw = DahuaSirenBinarySwitch(mock_coordinator, mock_config_entry)
        assert sw.name == "Siren"

    def test_icon(self, mock_coordinator, mock_config_entry):
        sw = DahuaSirenBinarySwitch(mock_coordinator, mock_config_entry)
        assert sw.icon == "mdi:bullhorn"

    def test_is_on(self, mock_coordinator, mock_config_entry):
        mock_coordinator.data = {"status.status.Speaker": "On"}
        sw = DahuaSirenBinarySwitch(mock_coordinator, mock_config_entry)
        assert sw.is_on is True

    @pytest.mark.asyncio
    async def test_turn_on(self, mock_coordinator, mock_config_entry):
        mock_coordinator.client.async_set_coaxial_control_state = AsyncMock()
        mock_coordinator.async_refresh = AsyncMock()
        sw = DahuaSirenBinarySwitch(mock_coordinator, mock_config_entry)

        await sw.async_turn_on()
        mock_coordinator.client.async_set_coaxial_control_state.assert_called_once_with(
            0, 2, True
        )

    @pytest.mark.asyncio
    async def test_turn_off(self, mock_coordinator, mock_config_entry):
        mock_coordinator.client.async_set_coaxial_control_state = AsyncMock()
        mock_coordinator.async_refresh = AsyncMock()
        sw = DahuaSirenBinarySwitch(mock_coordinator, mock_config_entry)

        await sw.async_turn_off()
        mock_coordinator.client.async_set_coaxial_control_state.assert_called_once_with(
            0, 2, False
        )


class TestSmartMotionDetectionSwitch:
    def test_unique_id(self, mock_coordinator, mock_config_entry):
        sw = DahuaSmartMotionDetectionBinarySwitch(mock_coordinator, mock_config_entry)
        assert sw.unique_id == "SERIAL123_smart_motion_detection"

    def test_name(self, mock_coordinator, mock_config_entry):
        sw = DahuaSmartMotionDetectionBinarySwitch(mock_coordinator, mock_config_entry)
        assert sw.name == "Smart Motion Detection"

    def test_icon(self, mock_coordinator, mock_config_entry):
        sw = DahuaSmartMotionDetectionBinarySwitch(mock_coordinator, mock_config_entry)
        assert sw.icon == "mdi:motion-sensor"

    def test_is_on_dahua(self, mock_coordinator, mock_config_entry):
        mock_coordinator.data = {"table.SmartMotionDetect[0].Enable": "true"}
        sw = DahuaSmartMotionDetectionBinarySwitch(mock_coordinator, mock_config_entry)
        assert sw.is_on is True

    @pytest.mark.asyncio
    async def test_turn_on_standard(self, mock_coordinator, mock_config_entry):
        """Standard Dahua uses async_enabled_smart_motion_detection."""
        mock_coordinator.model = "IPC-HDW5831R-ZE"
        mock_coordinator.client.async_enabled_smart_motion_detection = AsyncMock()
        mock_coordinator.async_refresh = AsyncMock()
        sw = DahuaSmartMotionDetectionBinarySwitch(mock_coordinator, mock_config_entry)

        await sw.async_turn_on()
        mock_coordinator.client.async_enabled_smart_motion_detection.assert_called_once_with(
            True
        )

    @pytest.mark.asyncio
    async def test_turn_on_amcrest(self, mock_coordinator, mock_config_entry):
        """Amcrest uses async_set_ivs_rule."""
        mock_coordinator.model = "AD410"
        mock_coordinator.client.async_set_ivs_rule = AsyncMock()
        mock_coordinator.async_refresh = AsyncMock()
        sw = DahuaSmartMotionDetectionBinarySwitch(mock_coordinator, mock_config_entry)

        await sw.async_turn_on()
        mock_coordinator.client.async_set_ivs_rule.assert_called_once_with(0, 0, True)

    @pytest.mark.asyncio
    async def test_turn_off_standard(self, mock_coordinator, mock_config_entry):
        mock_coordinator.model = "IPC-HDW5831R-ZE"
        mock_coordinator.client.async_enabled_smart_motion_detection = AsyncMock()
        mock_coordinator.async_refresh = AsyncMock()
        sw = DahuaSmartMotionDetectionBinarySwitch(mock_coordinator, mock_config_entry)

        await sw.async_turn_off()
        mock_coordinator.client.async_enabled_smart_motion_detection.assert_called_once_with(
            False
        )

    @pytest.mark.asyncio
    async def test_turn_off_amcrest(self, mock_coordinator, mock_config_entry):
        mock_coordinator.model = "AD410"
        mock_coordinator.client.async_set_ivs_rule = AsyncMock()
        mock_coordinator.async_refresh = AsyncMock()
        sw = DahuaSmartMotionDetectionBinarySwitch(mock_coordinator, mock_config_entry)

        await sw.async_turn_off()
        mock_coordinator.client.async_set_ivs_rule.assert_called_once_with(0, 0, False)


class TestDisarmingLinkageSwitch:
    def test_unique_id(self, mock_coordinator, mock_config_entry):
        sw = DahuaDisarmingLinkageBinarySwitch(mock_coordinator, mock_config_entry)
        assert sw.unique_id == "SERIAL123_disarming"

    def test_name(self, mock_coordinator, mock_config_entry):
        sw = DahuaDisarmingLinkageBinarySwitch(mock_coordinator, mock_config_entry)
        assert sw.name == "Disarming"

    def test_icon(self, mock_coordinator, mock_config_entry):
        sw = DahuaDisarmingLinkageBinarySwitch(mock_coordinator, mock_config_entry)
        assert sw.icon == "mdi:alarm-check"

    def test_is_on(self, mock_coordinator, mock_config_entry):
        mock_coordinator.data = {"table.DisableLinkage.Enable": "true"}
        sw = DahuaDisarmingLinkageBinarySwitch(mock_coordinator, mock_config_entry)
        assert sw.is_on is True

    @pytest.mark.asyncio
    async def test_turn_on(self, mock_coordinator, mock_config_entry):
        mock_coordinator.client.async_set_disarming_linkage = AsyncMock()
        mock_coordinator.async_refresh = AsyncMock()
        sw = DahuaDisarmingLinkageBinarySwitch(mock_coordinator, mock_config_entry)

        await sw.async_turn_on()
        mock_coordinator.client.async_set_disarming_linkage.assert_called_once_with(
            0, True
        )

    @pytest.mark.asyncio
    async def test_turn_off(self, mock_coordinator, mock_config_entry):
        mock_coordinator.client.async_set_disarming_linkage = AsyncMock()
        mock_coordinator.async_refresh = AsyncMock()
        sw = DahuaDisarmingLinkageBinarySwitch(mock_coordinator, mock_config_entry)

        await sw.async_turn_off()
        mock_coordinator.client.async_set_disarming_linkage.assert_called_once_with(
            0, False
        )


class TestEventNotificationsSwitch:
    def test_unique_id(self, mock_coordinator, mock_config_entry):
        sw = DahuaDisarmingEventNotificationsLinkageBinarySwitch(
            mock_coordinator, mock_config_entry
        )
        assert sw.unique_id == "SERIAL123_event_notifications"

    def test_name(self, mock_coordinator, mock_config_entry):
        sw = DahuaDisarmingEventNotificationsLinkageBinarySwitch(
            mock_coordinator, mock_config_entry
        )
        assert sw.name == "Event Notifications"

    def test_icon(self, mock_coordinator, mock_config_entry):
        sw = DahuaDisarmingEventNotificationsLinkageBinarySwitch(
            mock_coordinator, mock_config_entry
        )
        assert sw.icon == "mdi:bell-ring"

    @pytest.mark.asyncio
    async def test_turn_off(self, mock_coordinator, mock_config_entry):
        mock_coordinator.client.async_set_event_notifications = AsyncMock()
        mock_coordinator.async_refresh = AsyncMock()
        sw = DahuaDisarmingEventNotificationsLinkageBinarySwitch(
            mock_coordinator, mock_config_entry
        )

        await sw.async_turn_off()
        mock_coordinator.client.async_set_event_notifications.assert_called_once_with(
            0, False
        )

    def test_is_on(self, mock_coordinator, mock_config_entry):
        mock_coordinator.data = {"table.DisableEventNotify.Enable": "false"}
        sw = DahuaDisarmingEventNotificationsLinkageBinarySwitch(
            mock_coordinator, mock_config_entry
        )
        assert sw.is_on is True

    @pytest.mark.asyncio
    async def test_turn_on(self, mock_coordinator, mock_config_entry):
        mock_coordinator.client.async_set_event_notifications = AsyncMock()
        mock_coordinator.async_refresh = AsyncMock()
        sw = DahuaDisarmingEventNotificationsLinkageBinarySwitch(
            mock_coordinator, mock_config_entry
        )

        await sw.async_turn_on()
        mock_coordinator.client.async_set_event_notifications.assert_called_once_with(
            0, True
        )
