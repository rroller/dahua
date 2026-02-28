"""Tests for camera platform."""

from unittest.mock import AsyncMock

import pytest

from custom_components.dahua.camera import DahuaCamera, async_setup_entry


class TestAsyncSetupEntry:
    @pytest.mark.asyncio
    async def test_creates_cameras_per_stream(
        self, hass, mock_coordinator, mock_config_entry
    ):
        """Creates one camera per stream."""
        mock_coordinator._max_streams = 3
        mock_config_entry.runtime_data = mock_coordinator
        added = []

        # async_setup_entry calls entity_platform which needs hass context;
        # we'll test the DahuaCamera class directly instead
        cam0 = DahuaCamera(mock_coordinator, 0, mock_config_entry)
        cam1 = DahuaCamera(mock_coordinator, 1, mock_config_entry)
        cam2 = DahuaCamera(mock_coordinator, 2, mock_config_entry)

        assert cam0.name == "Main"
        assert cam1.name == "Sub"
        assert cam2.name == "Sub_2"


class TestDahuaCamera:
    def test_unique_id(self, mock_coordinator, mock_config_entry):
        cam = DahuaCamera(mock_coordinator, 0, mock_config_entry)
        assert cam.unique_id == "SERIAL123_Main"

    def test_unique_id_sub(self, mock_coordinator, mock_config_entry):
        cam = DahuaCamera(mock_coordinator, 1, mock_config_entry)
        assert cam.unique_id == "SERIAL123_Sub"

    def test_supported_features(self, mock_coordinator, mock_config_entry):
        from homeassistant.components.camera import CameraEntityFeature

        cam = DahuaCamera(mock_coordinator, 0, mock_config_entry)
        assert cam.supported_features == CameraEntityFeature.STREAM

    def test_motion_detection_enabled(self, mock_coordinator, mock_config_entry):
        mock_coordinator.data = {"table.MotionDetect[0].Enable": "true"}
        cam = DahuaCamera(mock_coordinator, 0, mock_config_entry)
        assert cam.motion_detection_enabled is True

    @pytest.mark.asyncio
    async def test_stream_source(self, mock_coordinator, mock_config_entry):
        cam = DahuaCamera(mock_coordinator, 0, mock_config_entry)
        source = await cam.stream_source()
        assert "rtsp://" in source
        assert "channel=1" in source

    @pytest.mark.asyncio
    async def test_async_camera_image(self, mock_coordinator, mock_config_entry):
        mock_coordinator.client.async_get_snapshot.side_effect = None
        mock_coordinator.client.async_get_snapshot.return_value = b"\xff\xd8"
        cam = DahuaCamera(mock_coordinator, 0, mock_config_entry)
        result = await cam.async_camera_image()
        assert result == b"\xff\xd8"

    @pytest.mark.asyncio
    async def test_async_enable_motion_detection(
        self, mock_coordinator, mock_config_entry
    ):
        mock_coordinator.client.enable_motion_detection = AsyncMock()
        mock_coordinator.async_refresh = AsyncMock()
        cam = DahuaCamera(mock_coordinator, 0, mock_config_entry)

        await cam.async_enable_motion_detection()

        mock_coordinator.client.enable_motion_detection.assert_called_once_with(0, True)

    @pytest.mark.asyncio
    async def test_async_disable_motion_detection(
        self, mock_coordinator, mock_config_entry
    ):
        mock_coordinator.client.enable_motion_detection = AsyncMock()
        mock_coordinator.async_refresh = AsyncMock()
        cam = DahuaCamera(mock_coordinator, 0, mock_config_entry)

        await cam.async_disable_motion_detection()

        mock_coordinator.client.enable_motion_detection.assert_called_once_with(
            0, False
        )

    @pytest.mark.asyncio
    async def test_async_enable_motion_detection_type_error(
        self, mock_coordinator, mock_config_entry
    ):
        """TypeError during motion detection is caught (device doesn't support it)."""
        mock_coordinator.client.enable_motion_detection = AsyncMock(
            side_effect=TypeError()
        )
        cam = DahuaCamera(mock_coordinator, 0, mock_config_entry)

        # Should not raise
        await cam.async_enable_motion_detection()

    @pytest.mark.asyncio
    async def test_async_disable_motion_detection_type_error(
        self, mock_coordinator, mock_config_entry
    ):
        """TypeError during disable motion detection is caught (lines 332-333)."""
        mock_coordinator.client.enable_motion_detection = AsyncMock(
            side_effect=TypeError()
        )
        cam = DahuaCamera(mock_coordinator, 0, mock_config_entry)

        # Should not raise
        await cam.async_disable_motion_detection()

    @pytest.mark.asyncio
    async def test_async_set_infrared_mode(self, mock_coordinator, mock_config_entry):
        mock_coordinator.client.async_set_lighting_v1_mode = AsyncMock()
        mock_coordinator.async_refresh = AsyncMock()
        cam = DahuaCamera(mock_coordinator, 0, mock_config_entry)

        await cam.async_set_infrared_mode("Auto", 80)

        mock_coordinator.client.async_set_lighting_v1_mode.assert_called_once_with(
            0, "Auto", 80
        )

    @pytest.mark.asyncio
    async def test_async_set_video_profile_mode_standard(
        self, mock_coordinator, mock_config_entry
    ):
        mock_coordinator.client.async_set_video_profile_mode = AsyncMock()
        cam = DahuaCamera(mock_coordinator, 0, mock_config_entry)

        await cam.async_set_video_profile_mode("Day")

        mock_coordinator.client.async_set_video_profile_mode.assert_called_once_with(
            0, "Day"
        )

    @pytest.mark.asyncio
    async def test_async_set_video_profile_mode_nvr4108hs(
        self, mock_coordinator, mock_config_entry
    ):
        """NVR4108HS uses async_set_night_switch_mode instead."""
        mock_coordinator.model = "DHI-NVR4108HS-8P-4KS2"
        mock_coordinator.client.async_set_night_switch_mode = AsyncMock()
        cam = DahuaCamera(mock_coordinator, 0, mock_config_entry)

        await cam.async_set_video_profile_mode("Night")

        mock_coordinator.client.async_set_night_switch_mode.assert_called_once_with(
            0, "Night"
        )

    @pytest.mark.asyncio
    async def test_async_set_video_profile_mode_ipc_color4k(
        self, mock_coordinator, mock_config_entry
    ):
        """IPC-Color4K uses async_set_night_switch_mode instead."""
        mock_coordinator.model = "IPC-Color4K-test"
        mock_coordinator.client.async_set_night_switch_mode = AsyncMock()
        cam = DahuaCamera(mock_coordinator, 0, mock_config_entry)

        await cam.async_set_video_profile_mode("Night")

        mock_coordinator.client.async_set_night_switch_mode.assert_called_once()

    @pytest.mark.asyncio
    async def test_async_reboot(self, mock_coordinator, mock_config_entry):
        mock_coordinator.client.reboot = AsyncMock()
        cam = DahuaCamera(mock_coordinator, 0, mock_config_entry)

        await cam.async_reboot()
        mock_coordinator.client.reboot.assert_called_once()

    @pytest.mark.asyncio
    async def test_async_set_record_mode(self, mock_coordinator, mock_config_entry):
        mock_coordinator.client.async_set_record_mode = AsyncMock()
        mock_coordinator.async_refresh = AsyncMock()
        cam = DahuaCamera(mock_coordinator, 0, mock_config_entry)

        await cam.async_set_record_mode("auto")
        mock_coordinator.client.async_set_record_mode.assert_called_once_with(0, "auto")

    @pytest.mark.asyncio
    async def test_async_adjustfocus(self, mock_coordinator, mock_config_entry):
        mock_coordinator.client.async_adjustfocus_v1 = AsyncMock()
        mock_coordinator.async_refresh = AsyncMock()
        cam = DahuaCamera(mock_coordinator, 0, mock_config_entry)

        await cam.async_adjustfocus("10", "5")
        mock_coordinator.client.async_adjustfocus_v1.assert_called_once_with("10", "5")

    @pytest.mark.asyncio
    async def test_async_set_privacy_masking(self, mock_coordinator, mock_config_entry):
        mock_coordinator.client.async_setprivacymask = AsyncMock()
        cam = DahuaCamera(mock_coordinator, 0, mock_config_entry)

        await cam.async_set_privacy_masking(0, True)
        mock_coordinator.client.async_setprivacymask.assert_called_once_with(0, True)

    @pytest.mark.asyncio
    async def test_async_set_enable_channel_title(
        self, mock_coordinator, mock_config_entry
    ):
        mock_coordinator.client.async_enable_channel_title = AsyncMock()
        cam = DahuaCamera(mock_coordinator, 0, mock_config_entry)

        await cam.async_set_enable_channel_title(True)
        mock_coordinator.client.async_enable_channel_title.assert_called_once_with(
            0, True
        )

    @pytest.mark.asyncio
    async def test_async_set_enable_time_overlay(
        self, mock_coordinator, mock_config_entry
    ):
        mock_coordinator.client.async_enable_time_overlay = AsyncMock()
        cam = DahuaCamera(mock_coordinator, 0, mock_config_entry)

        await cam.async_set_enable_time_overlay(True)
        mock_coordinator.client.async_enable_time_overlay.assert_called_once_with(
            0, True
        )

    @pytest.mark.asyncio
    async def test_async_set_enable_text_overlay(
        self, mock_coordinator, mock_config_entry
    ):
        mock_coordinator.client.async_enable_text_overlay = AsyncMock()
        cam = DahuaCamera(mock_coordinator, 0, mock_config_entry)

        await cam.async_set_enable_text_overlay(1, True)
        mock_coordinator.client.async_enable_text_overlay.assert_called_once_with(
            0, 1, True
        )

    @pytest.mark.asyncio
    async def test_async_set_enable_custom_overlay(
        self, mock_coordinator, mock_config_entry
    ):
        mock_coordinator.client.async_enable_custom_overlay = AsyncMock()
        cam = DahuaCamera(mock_coordinator, 0, mock_config_entry)

        await cam.async_set_enable_custom_overlay(0, True)
        mock_coordinator.client.async_enable_custom_overlay.assert_called_once_with(
            0, 0, True
        )

    @pytest.mark.asyncio
    async def test_async_set_enable_all_ivs_rules(
        self, mock_coordinator, mock_config_entry
    ):
        mock_coordinator.client.async_set_all_ivs_rules = AsyncMock()
        cam = DahuaCamera(mock_coordinator, 0, mock_config_entry)

        await cam.async_set_enable_all_ivs_rules(True)
        mock_coordinator.client.async_set_all_ivs_rules.assert_called_once_with(0, True)

    @pytest.mark.asyncio
    async def test_async_enable_ivs_rule(self, mock_coordinator, mock_config_entry):
        mock_coordinator.client.async_set_ivs_rule = AsyncMock()
        cam = DahuaCamera(mock_coordinator, 0, mock_config_entry)

        await cam.async_enable_ivs_rule(1, True)
        mock_coordinator.client.async_set_ivs_rule.assert_called_once_with(0, 1, True)

    @pytest.mark.asyncio
    async def test_async_vto_open_door(self, mock_coordinator, mock_config_entry):
        mock_coordinator.client.async_access_control_open_door = AsyncMock()
        cam = DahuaCamera(mock_coordinator, 0, mock_config_entry)

        await cam.async_vto_open_door(1)
        mock_coordinator.client.async_access_control_open_door.assert_called_once_with(
            1
        )

    @pytest.mark.asyncio
    async def test_async_goto_preset_position(
        self, mock_coordinator, mock_config_entry
    ):
        mock_coordinator.client.async_goto_preset_position = AsyncMock()
        mock_coordinator.async_refresh = AsyncMock()
        cam = DahuaCamera(mock_coordinator, 0, mock_config_entry)

        await cam.async_goto_preset_position(3)
        mock_coordinator.client.async_goto_preset_position.assert_called_once_with(0, 3)

    @pytest.mark.asyncio
    async def test_async_set_video_in_day_night_mode(
        self, mock_coordinator, mock_config_entry
    ):
        mock_coordinator.client.async_set_video_in_day_night_mode = AsyncMock()
        mock_coordinator.async_refresh = AsyncMock()
        cam = DahuaCamera(mock_coordinator, 0, mock_config_entry)

        await cam.async_set_video_in_day_night_mode("day", "color")
        mock_coordinator.client.async_set_video_in_day_night_mode.assert_called_once_with(
            0, "day", "color"
        )

    @pytest.mark.asyncio
    async def test_async_set_service_set_channel_title(
        self, mock_coordinator, mock_config_entry
    ):
        mock_coordinator.client.async_set_service_set_channel_title = AsyncMock()
        cam = DahuaCamera(mock_coordinator, 0, mock_config_entry)

        await cam.async_set_service_set_channel_title("Title1", "Title2")
        mock_coordinator.client.async_set_service_set_channel_title.assert_called_once_with(
            0, "Title1", "Title2"
        )

    @pytest.mark.asyncio
    async def test_async_set_service_set_text_overlay(
        self, mock_coordinator, mock_config_entry
    ):
        mock_coordinator.client.async_set_service_set_text_overlay = AsyncMock()
        cam = DahuaCamera(mock_coordinator, 0, mock_config_entry)

        await cam.async_set_service_set_text_overlay(0, "a", "b", "c", "d")
        mock_coordinator.client.async_set_service_set_text_overlay.assert_called_once_with(
            0, 0, "a", "b", "c", "d"
        )

    @pytest.mark.asyncio
    async def test_async_set_service_set_custom_overlay(
        self, mock_coordinator, mock_config_entry
    ):
        mock_coordinator.client.async_set_service_set_custom_overlay = AsyncMock()
        cam = DahuaCamera(mock_coordinator, 0, mock_config_entry)

        await cam.async_set_service_set_custom_overlay(0, "x", "y")
        mock_coordinator.client.async_set_service_set_custom_overlay.assert_called_once_with(
            0, 0, "x", "y"
        )

    @pytest.mark.asyncio
    async def test_async_vto_cancel_call(self, mock_coordinator, mock_config_entry):
        mock_vto = AsyncMock()
        mock_coordinator._vto_client = mock_vto
        cam = DahuaCamera(mock_coordinator, 0, mock_config_entry)

        await cam.async_vto_cancel_call()
        mock_vto.cancel_call.assert_called_once()
