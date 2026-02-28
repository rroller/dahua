"""Tests for coordinator initialization and setup/unload."""

from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from homeassistant.exceptions import PlatformNotReady
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.dahua import (
    async_setup_entry,
    async_unload_entry,
)


def _clear_polling_side_effects(mock_client):
    """Clear side_effects on client methods that are called during polling."""
    mock_client.async_get_config_lighting.side_effect = None
    mock_client.async_get_config_lighting.return_value = {
        "table.Lighting[0][0].Mode": "Auto"
    }
    mock_client.async_get_lighting_v2.side_effect = None
    mock_client.async_get_lighting_v2.return_value = {}
    mock_client.async_get_disarming_linkage.side_effect = None
    mock_client.async_get_disarming_linkage.return_value = {}
    mock_client.async_get_event_notifications.side_effect = None
    mock_client.async_get_event_notifications.return_value = {}
    mock_client.async_get_coaxial_control_io_status.side_effect = None
    mock_client.async_get_coaxial_control_io_status.return_value = {}
    mock_client.async_get_smart_motion_detection.side_effect = None
    mock_client.async_get_smart_motion_detection.return_value = {}
    mock_client.async_get_ptz_position.side_effect = None
    mock_client.async_get_ptz_position.return_value = {}
    mock_client.async_get_video_in_mode.return_value = {}
    mock_client.async_get_light_global_enabled.return_value = {}
    mock_client.async_get_video_analyse_rules_for_amcrest.return_value = {}


# --- _async_update_data initialization path ---


class TestAsyncUpdateDataInit:
    @pytest.mark.asyncio
    async def test_success_path(self, mock_coordinator, mock_client):
        """Test that initialization succeeds and sets initialized=True."""
        mock_coordinator.initialized = False

        # Enable capabilities for coverage
        mock_client.async_get_coaxial_control_io_status.side_effect = None
        mock_client.async_get_coaxial_control_io_status.return_value = {}
        mock_client.async_get_disarming_linkage.side_effect = None
        mock_client.async_get_disarming_linkage.return_value = {}
        mock_client.async_get_event_notifications.side_effect = None
        mock_client.async_get_event_notifications.return_value = {}
        mock_client.async_get_ptz_position.side_effect = None
        mock_client.async_get_ptz_position.return_value = {}
        mock_client.async_get_smart_motion_detection.side_effect = None
        mock_client.async_get_smart_motion_detection.return_value = {}
        mock_client.async_get_config_lighting.side_effect = None
        mock_client.async_get_config_lighting.return_value = {}
        mock_client.async_get_lighting_v2.side_effect = None
        mock_client.async_get_lighting_v2.return_value = {}
        mock_client.async_get_config.side_effect = aiohttp.ClientError()

        # Mock the event listener start (it creates asyncio tasks)
        with patch.object(
            mock_coordinator,
            "async_start_event_listener",
            new_callable=AsyncMock,
        ):
            await mock_coordinator._async_update_data()

        assert mock_coordinator.initialized is True
        assert mock_coordinator._supports_coaxial_control is True
        assert mock_coordinator._supports_disarming_linkage is True
        assert mock_coordinator._supports_smart_motion_detection is True
        assert mock_coordinator._supports_lighting_v2 is True

    @pytest.mark.asyncio
    async def test_coaxial_not_supported(self, mock_coordinator, mock_client):
        """Coaxial control detection sets flag to False on error."""
        mock_coordinator.initialized = False
        # coaxial already has side_effect from conftest - keep it as-is for init detection
        # Clear lighting so polling doesn't fail (supports_infrared_light needs _supports_lighting)
        mock_client.async_get_config_lighting.side_effect = None
        mock_client.async_get_config_lighting.return_value = {}
        mock_client.async_get_lighting_v2.side_effect = None
        mock_client.async_get_lighting_v2.return_value = {}
        mock_client.async_get_config.side_effect = aiohttp.ClientError()

        with patch.object(
            mock_coordinator,
            "async_start_event_listener",
            new_callable=AsyncMock,
        ):
            await mock_coordinator._async_update_data()

        assert mock_coordinator._supports_coaxial_control is False

    @pytest.mark.asyncio
    async def test_disarming_not_supported(self, mock_coordinator, mock_client):
        """Disarming linkage detection sets flag to False on error."""
        mock_coordinator.initialized = False
        mock_client.async_get_config_lighting.side_effect = None
        mock_client.async_get_config_lighting.return_value = {}
        mock_client.async_get_lighting_v2.side_effect = None
        mock_client.async_get_lighting_v2.return_value = {}
        mock_client.async_get_config.side_effect = aiohttp.ClientError()

        with patch.object(
            mock_coordinator,
            "async_start_event_listener",
            new_callable=AsyncMock,
        ):
            await mock_coordinator._async_update_data()

        assert mock_coordinator._supports_disarming_linkage is False

    @pytest.mark.asyncio
    async def test_smart_motion_not_supported(self, mock_coordinator, mock_client):
        """Smart motion detection sets flag to False on error."""
        mock_coordinator.initialized = False
        mock_client.async_get_config_lighting.side_effect = None
        mock_client.async_get_config_lighting.return_value = {}
        mock_client.async_get_lighting_v2.side_effect = None
        mock_client.async_get_lighting_v2.return_value = {}
        mock_client.async_get_config.side_effect = aiohttp.ClientError()

        with patch.object(
            mock_coordinator,
            "async_start_event_listener",
            new_callable=AsyncMock,
        ):
            await mock_coordinator._async_update_data()

        assert mock_coordinator._supports_smart_motion_detection is False

    @pytest.mark.asyncio
    async def test_ptz_not_supported(self, mock_coordinator, mock_client):
        """PTZ detection sets flag to False on error."""
        mock_coordinator.initialized = False
        mock_client.async_get_config_lighting.side_effect = None
        mock_client.async_get_config_lighting.return_value = {}
        mock_client.async_get_lighting_v2.side_effect = None
        mock_client.async_get_lighting_v2.return_value = {}
        mock_client.async_get_config.side_effect = aiohttp.ClientError()

        with patch.object(
            mock_coordinator,
            "async_start_event_listener",
            new_callable=AsyncMock,
        ):
            await mock_coordinator._async_update_data()

        assert mock_coordinator._supports_ptz_position is False

    @pytest.mark.asyncio
    async def test_lighting_not_supported(self, mock_coordinator, mock_client):
        """Lighting detection sets flag to False on error."""
        mock_coordinator.initialized = False
        # Keep async_get_config_lighting side_effect=ClientError from conftest
        mock_client.async_get_lighting_v2.side_effect = None
        mock_client.async_get_lighting_v2.return_value = {}
        mock_client.async_get_config.side_effect = aiohttp.ClientError()

        with patch.object(
            mock_coordinator,
            "async_start_event_listener",
            new_callable=AsyncMock,
        ):
            await mock_coordinator._async_update_data()

        # _supports_lighting stays False because async_get_config_lighting raised ClientError
        assert mock_coordinator._supports_lighting is False

    @pytest.mark.asyncio
    async def test_lighting_v2_not_supported(self, mock_coordinator, mock_client):
        """Lighting V2 detection sets flag to False on error."""
        mock_coordinator.initialized = False
        mock_client.async_get_config_lighting.side_effect = None
        mock_client.async_get_config_lighting.return_value = {}
        # Keep async_get_lighting_v2 side_effect=ClientError from conftest
        mock_client.async_get_config.side_effect = aiohttp.ClientError()

        with patch.object(
            mock_coordinator,
            "async_start_event_listener",
            new_callable=AsyncMock,
        ):
            await mock_coordinator._async_update_data()

        assert mock_coordinator._supports_lighting_v2 is False

    @pytest.mark.asyncio
    async def test_auth_failure_triggers_reauth(self, mock_coordinator, mock_client):
        """401 response triggers reauth flow."""
        mock_coordinator.initialized = False

        req_info = MagicMock()
        req_info.real_url = "http://test"
        mock_client.get_max_extra_streams.side_effect = aiohttp.ClientResponseError(
            req_info, (), status=401
        )
        mock_coordinator.config_entry.async_start_reauth = MagicMock()

        with pytest.raises(UpdateFailed, match="Authentication failed"):
            await mock_coordinator._async_update_data()

        mock_coordinator.config_entry.async_start_reauth.assert_called_once()

    @pytest.mark.asyncio
    async def test_generic_failure_raises_platform_not_ready(
        self, mock_coordinator, mock_client
    ):
        """Generic exception during init raises PlatformNotReady."""
        mock_coordinator.initialized = False
        mock_client.get_max_extra_streams.side_effect = RuntimeError("boom")

        with pytest.raises(PlatformNotReady):
            await mock_coordinator._async_update_data()

    @pytest.mark.asyncio
    async def test_channel_number_adjustment(self, mock_coordinator, mock_client):
        """When snapshot at index 0 works and not a doorbell, channel_number = channel."""
        mock_coordinator.initialized = False
        mock_client.async_get_snapshot.side_effect = None
        mock_client.async_get_snapshot.return_value = b"\xff"
        mock_coordinator._channel = 2
        mock_coordinator._channel_number = 3
        _clear_polling_side_effects(mock_client)

        with patch.object(
            mock_coordinator,
            "async_start_event_listener",
            new_callable=AsyncMock,
        ):
            await mock_coordinator._async_update_data()

        assert mock_coordinator._channel_number == 2

    @pytest.mark.asyncio
    async def test_doorbell_starts_vto_listener(self, mock_coordinator, mock_client):
        """Doorbells should start VTO event listener instead of regular."""
        mock_coordinator.initialized = False
        # Make sys_info return doorbell model
        mock_client.async_get_system_info.return_value = {
            "serialNumber": "SERIAL123",
            "deviceType": "VTO2101E-P",
        }
        _clear_polling_side_effects(mock_client)

        with patch.object(
            mock_coordinator,
            "async_start_vto_event_listener",
            new_callable=AsyncMock,
        ) as mock_vto:
            await mock_coordinator._async_update_data()

        mock_vto.assert_called_once()

    @pytest.mark.asyncio
    async def test_device_type_from_update_serial(self, mock_coordinator, mock_client):
        """When deviceType is 31, model comes from updateSerial."""
        mock_coordinator.initialized = False
        mock_client.async_get_system_info.return_value = {
            "serialNumber": "SERIAL123",
            "deviceType": "31",
            "updateSerial": "DHI-NVR4108HS-8P-4KS2",
        }
        _clear_polling_side_effects(mock_client)

        with patch.object(
            mock_coordinator,
            "async_start_event_listener",
            new_callable=AsyncMock,
        ):
            await mock_coordinator._async_update_data()

        assert mock_coordinator.model == "DHI-NVR4108HS-8P-4KS2"

    @pytest.mark.asyncio
    async def test_device_type_from_api(self, mock_coordinator, mock_client):
        """When deviceType is IP Camera and no updateSerial, calls get_device_type API."""
        mock_coordinator.initialized = False
        mock_client.async_get_system_info.return_value = {
            "serialNumber": "SERIAL123",
            "deviceType": "IP Camera",
        }
        mock_client.get_device_type.return_value = {"type": "IPC-XXXX"}
        _clear_polling_side_effects(mock_client)

        with patch.object(
            mock_coordinator,
            "async_start_event_listener",
            new_callable=AsyncMock,
        ):
            await mock_coordinator._async_update_data()

        assert mock_coordinator.model == "IPC-XXXX"


class TestAsyncUpdateDataPolling:
    @pytest.mark.asyncio
    async def test_basic_polling(self, mock_coordinator, mock_client):
        """Periodic polling gathers motion detection and returns data."""
        _clear_polling_side_effects(mock_client)

        data = await mock_coordinator._async_update_data()
        assert data.get("table.MotionDetect[0].Enable") == "true"

    @pytest.mark.asyncio
    async def test_polling_with_profile_mode(self, mock_coordinator, mock_client):
        """When profile mode is supported, it's fetched during polling."""
        _clear_polling_side_effects(mock_client)
        mock_coordinator._supports_profile_mode = True
        mock_client.async_get_video_in_mode.return_value = {
            "table.VideoInMode[0].Config[0]": "1"
        }

        await mock_coordinator._async_update_data()
        assert mock_coordinator._profile_mode == "1"

    @pytest.mark.asyncio
    async def test_polling_with_ptz(self, mock_coordinator, mock_client):
        """When PTZ is supported, position is fetched during polling."""
        _clear_polling_side_effects(mock_client)
        mock_coordinator._supports_ptz_position = True
        mock_client.async_get_ptz_position.return_value = {"status.PresetID": "3"}

        await mock_coordinator._async_update_data()
        assert mock_coordinator._preset_position == "3"

    @pytest.mark.asyncio
    async def test_polling_with_infrared(self, mock_coordinator, mock_client):
        """When infrared is supported, lighting config is fetched."""
        _clear_polling_side_effects(mock_client)
        mock_coordinator._supports_lighting = True
        mock_coordinator.model = "IPC-HDW5831R-ZE"

        data = await mock_coordinator._async_update_data()
        assert "table.Lighting[0][0].Mode" in data

    @pytest.mark.asyncio
    async def test_polling_failure_raises_update_failed(
        self, mock_coordinator, mock_client
    ):
        """Exception during polling raises UpdateFailed."""
        mock_client.async_get_config_motion_detection.side_effect = Exception(
            "network error"
        )

        with pytest.raises(UpdateFailed):
            await mock_coordinator._async_update_data()

    @pytest.mark.asyncio
    async def test_polling_with_disarming_linkage(self, mock_coordinator, mock_client):
        """When disarming linkage is supported, it's fetched."""
        _clear_polling_side_effects(mock_client)
        mock_coordinator._supports_disarming_linkage = True
        mock_client.async_get_disarming_linkage.return_value = {
            "table.DisableLinkage.Enable": "true"
        }

        data = await mock_coordinator._async_update_data()
        assert "table.DisableLinkage.Enable" in data

    @pytest.mark.asyncio
    async def test_polling_with_coaxial_control(self, mock_coordinator, mock_client):
        """When coaxial control is supported, status is fetched."""
        _clear_polling_side_effects(mock_client)
        mock_coordinator._supports_coaxial_control = True
        mock_client.async_get_coaxial_control_io_status.return_value = {
            "status.status.Speaker": "Off"
        }

        data = await mock_coordinator._async_update_data()
        assert "status.status.Speaker" in data

    @pytest.mark.asyncio
    async def test_polling_with_smart_motion(self, mock_coordinator, mock_client):
        """When smart motion is supported, it's fetched."""
        _clear_polling_side_effects(mock_client)
        mock_coordinator._supports_smart_motion_detection = True
        mock_client.async_get_smart_motion_detection.return_value = {
            "table.SmartMotionDetect[0].Enable": "true"
        }

        data = await mock_coordinator._async_update_data()
        assert "table.SmartMotionDetect[0].Enable" in data

    @pytest.mark.asyncio
    async def test_polling_with_lighting_v2(self, mock_coordinator, mock_client):
        """When lighting_v2 is supported, it's fetched."""
        _clear_polling_side_effects(mock_client)
        mock_coordinator._supports_lighting_v2 = True
        mock_client.async_get_lighting_v2.return_value = {
            "table.Lighting_V2[0][0][0].Mode": "Off"
        }

        data = await mock_coordinator._async_update_data()
        assert "table.Lighting_V2[0][0][0].Mode" in data

    @pytest.mark.asyncio
    async def test_polling_with_amcrest_doorbell(self, mock_coordinator, mock_client):
        """Amcrest doorbell fetches ring light status."""
        _clear_polling_side_effects(mock_client)
        mock_coordinator.model = "AD410"
        mock_client.async_get_light_global_enabled.return_value = {
            "table.LightGlobal[0].Enable": "true"
        }

        data = await mock_coordinator._async_update_data()
        assert "table.LightGlobal[0].Enable" in data

    @pytest.mark.asyncio
    async def test_polling_with_event_notifications(
        self, mock_coordinator, mock_client
    ):
        """When event notifications is supported, it's fetched."""
        _clear_polling_side_effects(mock_client)
        mock_coordinator._supports_event_notifications = True
        mock_client.async_get_event_notifications.return_value = {
            "table.DisableEventNotify.Enable": "false"
        }

        data = await mock_coordinator._async_update_data()
        assert "table.DisableEventNotify.Enable" in data

    @pytest.mark.asyncio
    async def test_polling_profile_mode_defaults_to_zero(
        self, mock_coordinator, mock_client
    ):
        """When profile mode returns empty, defaults to '0'."""
        _clear_polling_side_effects(mock_client)
        mock_coordinator._supports_profile_mode = True
        mock_client.async_get_video_in_mode.return_value = {
            "table.VideoInMode[0].Config[0]": ""
        }

        await mock_coordinator._async_update_data()
        assert mock_coordinator._profile_mode == "0"


# --- Setup / Unload ---


class TestAsyncSetupEntry:
    @pytest.mark.asyncio
    async def test_setup_success(self, hass, mock_config_entry, mock_client):
        """Test successful setup of the integration."""
        mock_config_entry.add_to_hass(hass)

        with (
            patch("custom_components.dahua.DahuaDataUpdateCoordinator") as MockCoord,
            patch.object(
                hass.config_entries,
                "async_forward_entry_setups",
                new_callable=AsyncMock,
            ),
        ):
            coord_instance = MagicMock()
            coord_instance.platforms = []
            coord_instance.last_update_success = True
            coord_instance.async_config_entry_first_refresh = AsyncMock()
            coord_instance.async_stop = AsyncMock()
            MockCoord.return_value = coord_instance

            result = await async_setup_entry(hass, mock_config_entry)

        assert result is True


class TestAsyncUnloadEntry:
    @pytest.mark.asyncio
    async def test_unload(self, hass, mock_config_entry, mock_coordinator):
        """Test unloading stops coordinator and unloads platforms."""
        mock_config_entry.add_to_hass(hass)
        mock_config_entry.runtime_data = mock_coordinator
        mock_coordinator.platforms = ["camera"]
        mock_coordinator.async_stop = AsyncMock()

        with patch.object(
            hass.config_entries,
            "async_forward_entry_unload",
            new_callable=AsyncMock,
            return_value=True,
        ):
            result = await async_unload_entry(hass, mock_config_entry)

        assert result is True
        mock_coordinator.async_stop.assert_called_once()
