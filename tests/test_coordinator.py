"""Tests for coordinator properties, model detection, state getters, and event handling."""

import time
from unittest.mock import MagicMock, patch

import pytest

# --- Model detection ---


class TestIsDoorbell:
    def test_vto_prefix(self, mock_coordinator):
        mock_coordinator.model = "VTO2101E-P"
        assert mock_coordinator.is_doorbell() is True

    def test_dh_vto_prefix(self, mock_coordinator):
        mock_coordinator.model = "DH-VTO2211G-P"
        assert mock_coordinator.is_doorbell() is True

    def test_dhi_non_nvr(self, mock_coordinator):
        mock_coordinator.model = "DHI-VTO2202F-P"
        assert mock_coordinator.is_doorbell() is True

    def test_dhi_nvr_is_not_doorbell(self, mock_coordinator):
        mock_coordinator.model = "DHI-NVR4108HS"
        assert mock_coordinator.is_doorbell() is False

    def test_amcrest_doorbell(self, mock_coordinator):
        mock_coordinator.model = "AD410"
        assert mock_coordinator.is_doorbell() is True

    def test_regular_ipc_is_not_doorbell(self, mock_coordinator):
        mock_coordinator.model = "IPC-HDW5831R-ZE"
        assert mock_coordinator.is_doorbell() is False


class TestIsAmcrestDoorbell:
    def test_ad_prefix(self, mock_coordinator):
        mock_coordinator.model = "AD410"
        assert mock_coordinator.is_amcrest_doorbell() is True

    def test_db6_prefix(self, mock_coordinator):
        mock_coordinator.model = "DB61i"
        assert mock_coordinator.is_amcrest_doorbell() is True

    def test_regular_ipc(self, mock_coordinator):
        mock_coordinator.model = "IPC-HDW5831R-ZE"
        assert mock_coordinator.is_amcrest_doorbell() is False


class TestIsFloodLight:
    def test_ash26(self, mock_coordinator):
        mock_coordinator.model = "ASH26-W"
        assert mock_coordinator.is_flood_light() is True

    def test_l26n(self, mock_coordinator):
        mock_coordinator.model = "IPC-L26N"
        assert mock_coordinator.is_flood_light() is True

    def test_l46n(self, mock_coordinator):
        mock_coordinator.model = "IPC-L46N"
        assert mock_coordinator.is_flood_light() is True

    def test_v261lc(self, mock_coordinator):
        mock_coordinator.model = "V261LC-W"
        assert mock_coordinator.is_flood_light() is True

    def test_w452asd(self, mock_coordinator):
        mock_coordinator.model = "W452ASD"
        assert mock_coordinator.is_flood_light() is True

    def test_regular_ipc(self, mock_coordinator):
        mock_coordinator.model = "IPC-HDW5831R-ZE"
        assert mock_coordinator.is_flood_light() is False


class TestSupportsSiren:
    def test_as_pv(self, mock_coordinator):
        mock_coordinator.model = "IPC-HDW3849HP-AS-PV"
        assert mock_coordinator.supports_siren() is True

    def test_l46n(self, mock_coordinator):
        mock_coordinator.model = "IPC-L46N"
        assert mock_coordinator.supports_siren() is True

    def test_w452asd(self, mock_coordinator):
        mock_coordinator.model = "W452ASD"
        assert mock_coordinator.supports_siren() is True

    def test_regular_ipc(self, mock_coordinator):
        mock_coordinator.model = "IPC-HDW5831R-ZE"
        assert mock_coordinator.supports_siren() is False


class TestSupportsInfraredLight:
    def test_regular_ipc_with_lighting(self, mock_coordinator):
        mock_coordinator._supports_lighting = True
        mock_coordinator.model = "IPC-HDW5831R-ZE"
        assert mock_coordinator.supports_infrared_light() is True

    def test_as_pv_excluded(self, mock_coordinator):
        mock_coordinator._supports_lighting = True
        mock_coordinator.model = "IPC-HDW3849HP-AS-PV"
        assert mock_coordinator.supports_infrared_light() is False

    def test_as_ni_excluded(self, mock_coordinator):
        mock_coordinator._supports_lighting = True
        mock_coordinator.model = "IPC-HDW3849HP-AS-NI"
        assert mock_coordinator.supports_infrared_light() is False

    def test_led_s2_excluded(self, mock_coordinator):
        mock_coordinator._supports_lighting = True
        mock_coordinator.model = "IPC-HFW2439SP-SA-LED-S2"
        assert mock_coordinator.supports_infrared_light() is False

    def test_no_lighting_support(self, mock_coordinator):
        mock_coordinator._supports_lighting = False
        mock_coordinator.model = "IPC-HDW5831R-ZE"
        assert mock_coordinator.supports_infrared_light() is False


class TestSupportsSecurityLight:
    def test_as_pv(self, mock_coordinator):
        mock_coordinator.model = "IPC-HDW3849HP-AS-PV"
        assert mock_coordinator.supports_security_light() is True

    def test_ad410(self, mock_coordinator):
        mock_coordinator.model = "AD410"
        assert mock_coordinator.supports_security_light() is True

    def test_db61i(self, mock_coordinator):
        mock_coordinator.model = "DB61i"
        assert mock_coordinator.supports_security_light() is True

    def test_ip8m(self, mock_coordinator):
        mock_coordinator.model = "IP8M-2796E-B"
        assert mock_coordinator.supports_security_light() is True

    def test_regular_ipc(self, mock_coordinator):
        mock_coordinator.model = "IPC-HDW5831R-ZE"
        assert mock_coordinator.supports_security_light() is False


class TestSupportsFloodlightmode:
    def test_w452asd(self, mock_coordinator):
        mock_coordinator.model = "W452ASD"
        assert mock_coordinator.supports_floodlightmode() is True

    def test_l46n(self, mock_coordinator):
        mock_coordinator.model = "IPC-L46N"
        assert mock_coordinator.supports_floodlightmode() is True

    def test_regular(self, mock_coordinator):
        mock_coordinator.model = "IPC-HDW5831R-ZE"
        assert mock_coordinator.supports_floodlightmode() is False


class TestSupportsIlluminator:
    def test_with_lighting_v2_data(self, mock_coordinator):
        mock_coordinator.data = {"table.Lighting_V2[0][0][0].Mode": "Manual"}
        assert mock_coordinator.supports_illuminator() is True

    def test_without_lighting_v2_data(self, mock_coordinator):
        mock_coordinator.data = {}
        assert mock_coordinator.supports_illuminator() is False

    def test_amcrest_doorbell_excluded(self, mock_coordinator):
        mock_coordinator.model = "AD410"
        mock_coordinator.data = {"table.Lighting_V2[0][0][0].Mode": "Manual"}
        assert mock_coordinator.supports_illuminator() is False

    def test_flood_light_excluded(self, mock_coordinator):
        mock_coordinator.model = "ASH26-W"
        mock_coordinator.data = {"table.Lighting_V2[0][0][0].Mode": "Manual"}
        assert mock_coordinator.supports_illuminator() is False


class TestSupportsPtzPosition:
    def test_with_lighting_v2_data(self, mock_coordinator):
        mock_coordinator.data = {"table.Lighting_V2[0][0][0].Mode": "Manual"}
        assert mock_coordinator.supports_ptz_position() is True

    def test_without_data(self, mock_coordinator):
        mock_coordinator.data = {}
        assert mock_coordinator.supports_ptz_position() is False


class TestSupportsSmartMotionDetection:
    def test_supported(self, mock_coordinator):
        mock_coordinator._supports_smart_motion_detection = True
        assert mock_coordinator.supports_smart_motion_detection() is True

    def test_not_supported(self, mock_coordinator):
        mock_coordinator._supports_smart_motion_detection = False
        assert mock_coordinator.supports_smart_motion_detection() is False


class TestSupportsSmartMotionDetectionAmcrest:
    def test_ad410(self, mock_coordinator):
        mock_coordinator.model = "AD410"
        assert mock_coordinator.supports_smart_motion_detection_amcrest() is True

    def test_db61i(self, mock_coordinator):
        mock_coordinator.model = "DB61i"
        assert mock_coordinator.supports_smart_motion_detection_amcrest() is True

    def test_regular(self, mock_coordinator):
        mock_coordinator.model = "IPC-HDW5831R-ZE"
        assert mock_coordinator.supports_smart_motion_detection_amcrest() is False


# --- State getters ---


class TestIsMotionDetectionEnabled:
    def test_enabled(self, mock_coordinator):
        mock_coordinator.data = {"table.MotionDetect[0].Enable": "true"}
        assert mock_coordinator.is_motion_detection_enabled() is True

    def test_disabled(self, mock_coordinator):
        mock_coordinator.data = {"table.MotionDetect[0].Enable": "false"}
        assert mock_coordinator.is_motion_detection_enabled() is False

    def test_missing(self, mock_coordinator):
        mock_coordinator.data = {}
        assert mock_coordinator.is_motion_detection_enabled() is False


class TestIsDisarmingLinkageEnabled:
    def test_enabled(self, mock_coordinator):
        mock_coordinator.data = {"table.DisableLinkage.Enable": "true"}
        assert mock_coordinator.is_disarming_linkage_enabled() is True

    def test_disabled(self, mock_coordinator):
        mock_coordinator.data = {"table.DisableLinkage.Enable": "false"}
        assert mock_coordinator.is_disarming_linkage_enabled() is False


class TestIsEventNotificationsEnabled:
    def test_enabled_when_disable_is_false(self, mock_coordinator):
        """Event notifications are ON when DisableEventNotify.Enable is false (inverted)."""
        mock_coordinator.data = {"table.DisableEventNotify.Enable": "false"}
        assert mock_coordinator.is_event_notifications_enabled() is True

    def test_disabled_when_disable_is_true(self, mock_coordinator):
        mock_coordinator.data = {"table.DisableEventNotify.Enable": "true"}
        assert mock_coordinator.is_event_notifications_enabled() is False


class TestIsSmartMotionDetectionEnabled:
    def test_dahua_enabled(self, mock_coordinator):
        mock_coordinator.model = "IPC-HDW5831R-ZE"
        mock_coordinator.data = {"table.SmartMotionDetect[0].Enable": "true"}
        assert mock_coordinator.is_smart_motion_detection_enabled() is True

    def test_dahua_disabled(self, mock_coordinator):
        mock_coordinator.model = "IPC-HDW5831R-ZE"
        mock_coordinator.data = {"table.SmartMotionDetect[0].Enable": "false"}
        assert mock_coordinator.is_smart_motion_detection_enabled() is False

    def test_amcrest_enabled(self, mock_coordinator):
        mock_coordinator.model = "AD410"
        mock_coordinator.data = {"table.VideoAnalyseRule[0][0].Enable": "true"}
        assert mock_coordinator.is_smart_motion_detection_enabled() is True

    def test_amcrest_disabled(self, mock_coordinator):
        mock_coordinator.model = "AD410"
        mock_coordinator.data = {"table.VideoAnalyseRule[0][0].Enable": "false"}
        assert mock_coordinator.is_smart_motion_detection_enabled() is False


class TestIsSirenOn:
    def test_on(self, mock_coordinator):
        mock_coordinator.data = {"status.status.Speaker": "On"}
        assert mock_coordinator.is_siren_on() is True

    def test_off(self, mock_coordinator):
        mock_coordinator.data = {"status.status.Speaker": "Off"}
        assert mock_coordinator.is_siren_on() is False


class TestIsInfraredLightOn:
    def test_on(self, mock_coordinator):
        mock_coordinator.data = {"table.Lighting[0][0].Mode": "Manual"}
        assert mock_coordinator.is_infrared_light_on() is True

    def test_off(self, mock_coordinator):
        mock_coordinator.data = {"table.Lighting[0][0].Mode": "Off"}
        assert mock_coordinator.is_infrared_light_on() is False


class TestGetInfraredBrightness:
    def test_returns_converted_brightness(self, mock_coordinator):
        mock_coordinator.data = {"table.Lighting[0][0].MiddleLight[0].Light": "100"}
        assert mock_coordinator.get_infrared_brightness() == 255


class TestIsIlluminatorOn:
    def test_on(self, mock_coordinator):
        mock_coordinator._profile_mode = "0"
        mock_coordinator.data = {"table.Lighting_V2[0][0][0].Mode": "Manual"}
        assert mock_coordinator.is_illuminator_on() is True

    def test_off(self, mock_coordinator):
        mock_coordinator._profile_mode = "0"
        mock_coordinator.data = {"table.Lighting_V2[0][0][0].Mode": "Off"}
        assert mock_coordinator.is_illuminator_on() is False


class TestGetIlluminatorBrightness:
    def test_returns_converted_brightness(self, mock_coordinator):
        mock_coordinator.data = {
            "table.Lighting_V2[0][0][0].MiddleLight[0].Light": "50"
        }
        assert mock_coordinator.get_illuminator_brightness() == 127


class TestIsFloodLightOn:
    def test_on_with_floodlightmode(self, mock_coordinator):
        mock_coordinator._supports_floodlightmode = True
        mock_coordinator.data = {"status.status.WhiteLight": "On"}
        assert mock_coordinator.is_flood_light_on() is True

    def test_off_with_floodlightmode(self, mock_coordinator):
        mock_coordinator._supports_floodlightmode = True
        mock_coordinator.data = {"status.status.WhiteLight": "Off"}
        assert mock_coordinator.is_flood_light_on() is False

    def test_on_without_floodlightmode(self, mock_coordinator):
        mock_coordinator._supports_floodlightmode = False
        mock_coordinator._profile_mode = "0"
        mock_coordinator.data = {"table.Lighting_V2[0][0][1].Mode": "Manual"}
        assert mock_coordinator.is_flood_light_on() is True

    def test_off_without_floodlightmode(self, mock_coordinator):
        mock_coordinator._supports_floodlightmode = False
        mock_coordinator._profile_mode = "0"
        mock_coordinator.data = {"table.Lighting_V2[0][0][1].Mode": "Off"}
        assert mock_coordinator.is_flood_light_on() is False


class TestIsRingLightOn:
    def test_on(self, mock_coordinator):
        mock_coordinator.data = {"table.LightGlobal[0].Enable": "true"}
        assert mock_coordinator.is_ring_light_on() is True

    def test_off(self, mock_coordinator):
        mock_coordinator.data = {"table.LightGlobal[0].Enable": "false"}
        assert mock_coordinator.is_ring_light_on() is False


class TestIsSecurityLightOn:
    def test_on(self, mock_coordinator):
        mock_coordinator.data = {"status.status.WhiteLight": "On"}
        assert mock_coordinator.is_security_light_on() is True

    def test_off(self, mock_coordinator):
        mock_coordinator.data = {"status.status.WhiteLight": "Off"}
        assert mock_coordinator.is_security_light_on() is False


# --- Simple getters ---


class TestGetDeviceName:
    def test_returns_name(self, mock_coordinator):
        mock_coordinator._name = "Front Porch"
        assert mock_coordinator.get_device_name() == "Front Porch"

    def test_fallback_to_machine_name(self, mock_coordinator):
        mock_coordinator._name = None
        mock_coordinator.machine_name = "MachineCam"
        assert mock_coordinator.get_device_name() == "MachineCam"


class TestGetModel:
    def test_returns_model(self, mock_coordinator):
        assert mock_coordinator.get_model() == "IPC-HDW5831R-ZE"


class TestGetSerialNumber:
    def test_channel_0(self, mock_coordinator):
        mock_coordinator._channel = 0
        assert mock_coordinator.get_serial_number() == "SERIAL123"

    def test_channel_gt_0(self, mock_coordinator):
        mock_coordinator._channel = 1
        assert mock_coordinator.get_serial_number() == "SERIAL123_1"


class TestGetEventList:
    def test_returns_events(self, mock_coordinator):
        assert mock_coordinator.get_event_list() == [
            "VideoMotion",
            "CrossLineDetection",
        ]


class TestGetProfileMode:
    def test_returns_mode(self, mock_coordinator):
        mock_coordinator._profile_mode = "1"
        assert mock_coordinator.get_profile_mode() == "1"


class TestGetChannel:
    def test_returns_channel(self, mock_coordinator):
        assert mock_coordinator.get_channel() == 0


class TestGetMaxStreams:
    def test_returns_max(self, mock_coordinator):
        assert mock_coordinator.get_max_streams() == 3


class TestGetAddress:
    def test_returns_address(self, mock_coordinator):
        assert mock_coordinator.get_address() == "192.168.1.108"


# --- Event handling ---


class TestOnReceive:
    def test_video_motion_start(self, mock_coordinator):
        """VideoMotion Start should set timestamp and call listener."""
        called = []
        mock_coordinator.add_dahua_event_listener(
            "VideoMotion", lambda: called.append(True)
        )

        data = (
            b"--myboundary\n"
            b"Content-Type: text/plain\n"
            b"Content-Length: 50\n"
            b"\n"
            b"Code=VideoMotion;action=Start;index=0"
        )

        with patch("custom_components.dahua.time") as mock_time:
            mock_time.time.return_value = 1000
            mock_coordinator.on_receive(data, 0)

        assert len(called) == 1
        assert mock_coordinator.get_event_timestamp("VideoMotion") == 1000

    def test_video_motion_stop(self, mock_coordinator):
        """VideoMotion Stop should clear timestamp and call listener."""
        called = []
        mock_coordinator.add_dahua_event_listener(
            "VideoMotion", lambda: called.append(True)
        )

        # Set initial timestamp
        mock_coordinator._dahua_event_timestamp["VideoMotion-0"] = 1000

        data = (
            b"--myboundary\n"
            b"Content-Type: text/plain\n"
            b"Content-Length: 50\n"
            b"\n"
            b"Code=VideoMotion;action=Stop;index=0"
        )
        mock_coordinator.on_receive(data, 0)

        assert len(called) == 1
        assert mock_coordinator.get_event_timestamp("VideoMotion") == 0

    def test_wrong_channel_ignored(self, mock_coordinator):
        """Events for wrong channel should be ignored."""
        called = []
        mock_coordinator.add_dahua_event_listener(
            "VideoMotion", lambda: called.append(True)
        )
        mock_coordinator._channel = 0

        data = (
            b"--myboundary\n"
            b"Content-Type: text/plain\n"
            b"Content-Length: 50\n"
            b"\n"
            b"Code=VideoMotion;action=Start;index=1"
        )
        mock_coordinator.on_receive(data, 0)

        assert len(called) == 0


class TestTranslateEventCode:
    def test_crossline_human_to_smart_motion(self, mock_coordinator):
        """CrossLineDetection with Human ObjectType -> SmartMotionHuman when no CrossLine listener."""
        event = {
            "Code": "CrossLineDetection",
            "data": {"Object": {"ObjectType": "Human"}},
        }
        result = mock_coordinator.translate_event_code(event)
        assert result == "SmartMotionHuman"

    def test_crossline_human_with_listener(self, mock_coordinator):
        """CrossLineDetection with Human stays as-is when a CrossLine listener exists."""
        mock_coordinator.add_dahua_event_listener("CrossLineDetection", lambda: None)
        event = {
            "Code": "CrossLineDetection",
            "data": {"Object": {"ObjectType": "Human"}},
        }
        result = mock_coordinator.translate_event_code(event)
        assert result == "CrossLineDetection"

    def test_back_key_light_to_doorbell_pressed(self, mock_coordinator):
        event = {"Code": "BackKeyLight"}
        result = mock_coordinator.translate_event_code(event)
        assert result == "DoorbellPressed"

    def test_phone_call_detect_to_doorbell_pressed(self, mock_coordinator):
        event = {"Code": "PhoneCallDetect"}
        result = mock_coordinator.translate_event_code(event)
        assert result == "DoorbellPressed"

    def test_normal_passthrough(self, mock_coordinator):
        event = {"Code": "VideoMotion"}
        result = mock_coordinator.translate_event_code(event)
        assert result == "VideoMotion"


class TestOnReceiveVtoEvent:
    def test_start_action(self, mock_coordinator):
        """VTO event with Start action should set timestamp."""
        called = []
        mock_coordinator.add_dahua_event_listener(
            "VideoMotion", lambda: called.append(True)
        )

        event = {"Code": "VideoMotion", "Action": "Start", "Data": {}}

        with patch("custom_components.dahua.time") as mock_time:
            mock_time.time.return_value = 2000
            mock_coordinator.on_receive_vto_event(event)

        assert len(called) == 1
        assert mock_coordinator.get_event_timestamp("VideoMotion") == 2000

    def test_stop_action(self, mock_coordinator):
        """VTO event with Stop action should clear timestamp."""
        called = []
        mock_coordinator.add_dahua_event_listener(
            "VideoMotion", lambda: called.append(True)
        )
        mock_coordinator._dahua_event_timestamp["VideoMotion-0"] = 2000

        event = {"Code": "VideoMotion", "Action": "Stop", "Data": {}}
        mock_coordinator.on_receive_vto_event(event)

        assert len(called) == 1
        assert mock_coordinator.get_event_timestamp("VideoMotion") == 0

    def test_pulse_door_status_open(self, mock_coordinator):
        """DoorStatus Pulse with Open sets timestamp."""
        called = []
        mock_coordinator.add_dahua_event_listener(
            "DoorStatus", lambda: called.append(True)
        )

        event = {
            "Code": "DoorStatus",
            "Action": "Pulse",
            "Data": {"Status": "Open"},
        }

        with patch("custom_components.dahua.time") as mock_time:
            mock_time.time.return_value = 3000
            mock_coordinator.on_receive_vto_event(event)

        assert mock_coordinator.get_event_timestamp("DoorStatus") == 3000

    def test_pulse_door_status_close(self, mock_coordinator):
        """DoorStatus Pulse with Close clears timestamp."""
        called = []
        mock_coordinator.add_dahua_event_listener(
            "DoorStatus", lambda: called.append(True)
        )
        mock_coordinator._dahua_event_timestamp["DoorStatus-0"] = 3000

        event = {
            "Code": "DoorStatus",
            "Action": "Pulse",
            "Data": {"Status": "Close"},
        }
        mock_coordinator.on_receive_vto_event(event)

        assert mock_coordinator.get_event_timestamp("DoorStatus") == 0

    def test_pulse_button_pressed(self, mock_coordinator):
        """BackKeyLight Pulse with State=1 fires DoorbellPressed."""
        called = []
        mock_coordinator.add_dahua_event_listener(
            "DoorbellPressed", lambda: called.append(True)
        )

        event = {
            "Code": "BackKeyLight",
            "Action": "Pulse",
            "Data": {"State": 1},
        }

        with patch("custom_components.dahua.time") as mock_time:
            mock_time.time.return_value = 4000
            mock_coordinator.on_receive_vto_event(event)

        assert mock_coordinator.get_event_timestamp("DoorbellPressed") == 4000

    def test_pulse_button_released(self, mock_coordinator):
        """BackKeyLight Pulse with State=0 clears DoorbellPressed."""
        called = []
        mock_coordinator.add_dahua_event_listener(
            "DoorbellPressed", lambda: called.append(True)
        )
        mock_coordinator._dahua_event_timestamp["DoorbellPressed-0"] = 4000

        event = {
            "Code": "BackKeyLight",
            "Action": "Pulse",
            "Data": {"State": 0},
        }
        mock_coordinator.on_receive_vto_event(event)

        assert mock_coordinator.get_event_timestamp("DoorbellPressed") == 0


class TestGetEventTimestamp:
    def test_returns_timestamp(self, mock_coordinator):
        mock_coordinator._dahua_event_timestamp["VideoMotion-0"] = 1234
        assert mock_coordinator.get_event_timestamp("VideoMotion") == 1234

    def test_returns_zero_for_unknown(self, mock_coordinator):
        assert mock_coordinator.get_event_timestamp("Unknown") == 0


class TestAddDahuaEventListener:
    def test_registers_listener(self, mock_coordinator):
        listener = lambda: None
        mock_coordinator.add_dahua_event_listener("VideoMotion", listener)
        assert mock_coordinator._dahua_event_listeners.get("VideoMotion-0") is listener


# --- Cleanup ---


class TestAsyncStop:
    @pytest.mark.asyncio
    async def test_cancels_event_task(self, mock_coordinator):
        mock_task = MagicMock()
        mock_coordinator._event_task = mock_task
        await mock_coordinator.async_stop()
        mock_task.cancel.assert_called_once()
        assert mock_coordinator._event_task is None

    @pytest.mark.asyncio
    async def test_cancels_vto_task(self, mock_coordinator):
        mock_task = MagicMock()
        mock_coordinator._vto_task = mock_task
        await mock_coordinator.async_stop()
        mock_task.cancel.assert_called_once()
        assert mock_coordinator._vto_task is None

    @pytest.mark.asyncio
    async def test_no_tasks(self, mock_coordinator):
        """No error when no tasks to cancel."""
        await mock_coordinator.async_stop()


class TestCloseSession:
    @pytest.mark.asyncio
    async def test_closes_session(self, mock_coordinator):
        mock_session = mock_coordinator._session
        await mock_coordinator._close_session()
        mock_session.close.assert_called_once()
        assert mock_coordinator._session is None

    @pytest.mark.asyncio
    async def test_none_session(self, mock_coordinator):
        mock_coordinator._session = None
        await mock_coordinator._close_session()  # Should not raise

    @pytest.mark.asyncio
    async def test_close_session_exception(self, mock_coordinator):
        """Exception during session close is caught (line 267-268)."""
        from unittest.mock import AsyncMock

        mock_coordinator._session = AsyncMock()
        mock_coordinator._session.close.side_effect = Exception("close failed")
        await mock_coordinator._close_session()  # Should not raise


class TestOnReceiveExtended:
    def _make_event_data(self, event_str):
        """Format event string with boundary markers for parse_event."""
        return (
            "--myboundary\r\n"
            "Content-Type: text/plain\r\n"
            "Content-Length: 100\r\n"
            "\r\n"
            f"{event_str}"
        ).encode("utf-8")

    def test_event_with_index_parsing(self, mock_coordinator):
        """on_receive parses index from event data (lines 650-655)."""
        called = []
        mock_coordinator.add_dahua_event_listener(
            "CrossLineDetection", lambda: called.append(True)
        )

        data = self._make_event_data("Code=CrossLineDetection;action=Start;index=0")
        with patch("custom_components.dahua.time") as mock_time:
            mock_time.time.return_value = 5000
            mock_coordinator.on_receive(data, 0)

        assert len(called) == 1

    def test_event_with_invalid_index(self, mock_coordinator):
        """Non-integer index defaults to 0 (lines 654-655)."""
        called = []
        mock_coordinator.add_dahua_event_listener(
            "VideoMotion", lambda: called.append(True)
        )

        data = self._make_event_data("Code=VideoMotion;action=Start;index=abc")
        with patch("custom_components.dahua.time") as mock_time:
            mock_time.time.return_value = 5000
            mock_coordinator.on_receive(data, 0)

        assert len(called) == 1

    def test_empty_events_returns_early(self, mock_coordinator):
        """Empty data returns early without errors (line 643)."""
        mock_coordinator.on_receive(b"", 0)

    def test_event_wrong_channel_index(self, mock_coordinator):
        """Events with index != channel are discarded (lines 659)."""
        called = []
        mock_coordinator._channel = 0
        mock_coordinator.add_dahua_event_listener(
            "VideoMotion", lambda: called.append(True)
        )

        data = self._make_event_data("Code=VideoMotion;action=Start;index=1")
        mock_coordinator.on_receive(data, 0)

        assert len(called) == 0


class TestAsyncStartEventListener:
    @pytest.mark.asyncio
    async def test_creates_task(self, mock_coordinator):
        """async_start_event_listener creates a task when events exist (lines 187-188)."""
        import asyncio

        mock_coordinator.events = ["VideoMotion"]
        with patch.object(
            mock_coordinator, "_async_stream_events", return_value=asyncio.sleep(0)
        ):
            await mock_coordinator.async_start_event_listener()
            assert mock_coordinator._event_task is not None
            mock_coordinator._event_task.cancel()

    @pytest.mark.asyncio
    async def test_no_events_no_task(self, mock_coordinator):
        """No task created when events is None."""
        mock_coordinator.events = None
        await mock_coordinator.async_start_event_listener()
        assert mock_coordinator._event_task is None


class TestAsyncStartVtoEventListener:
    @pytest.mark.asyncio
    async def test_creates_task(self, mock_coordinator):
        """async_start_vto_event_listener creates a task (line 192)."""
        import asyncio

        with patch.object(
            mock_coordinator,
            "_async_stream_vto_events",
            return_value=asyncio.sleep(0),
        ):
            await mock_coordinator.async_start_vto_event_listener()
            assert mock_coordinator._vto_task is not None
            mock_coordinator._vto_task.cancel()
