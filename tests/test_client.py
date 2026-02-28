"""Tests for client.py (DahuaClient)."""

from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from custom_components.dahua.client import DahuaClient

# --- Constructor tests ---


class TestConstructor:
    def test_http_base_url(self):
        session = MagicMock()
        client = DahuaClient("admin", "pass", "192.168.1.1", 80, 554, session)
        assert client._base == "http://192.168.1.1:80"

    def test_https_for_port_443(self):
        session = MagicMock()
        client = DahuaClient("admin", "pass", "192.168.1.1", 443, 554, session)
        assert client._base == "https://192.168.1.1:443"

    def test_trailing_slash_stripped(self):
        session = MagicMock()
        client = DahuaClient("admin", "pass", "192.168.1.1/", 80, 554, session)
        assert client._address == "192.168.1.1"


# --- Static helpers ---


class TestToStreamName:
    def test_main_stream(self):
        assert DahuaClient.to_stream_name(0) == "Main"

    def test_sub_stream(self):
        assert DahuaClient.to_stream_name(1) == "Sub"

    def test_sub_2_stream(self):
        assert DahuaClient.to_stream_name(2) == "Sub_2"

    def test_sub_3_stream(self):
        assert DahuaClient.to_stream_name(3) == "Sub_3"


class TestGetRtspStreamUrl:
    def test_main_stream_url(self):
        session = MagicMock()
        client = DahuaClient("admin", "pass", "192.168.1.1", 80, 554, session)
        url = client.get_rtsp_stream_url(1, 0)
        assert (
            url
            == "rtsp://admin:pass@192.168.1.1:554/cam/realmonitor?channel=1&subtype=0"
        )

    def test_subtype_3_url(self):
        session = MagicMock()
        client = DahuaClient("admin", "pass", "192.168.1.1", 80, 554, session)
        url = client.get_rtsp_stream_url(1, 3)
        assert url == "rtsp://admin:pass@192.168.1.1"


# --- parse_dahua_api_response ---


class TestParseDahuaApiResponse:
    @pytest.mark.asyncio
    async def test_key_value_parsing(self):
        data = "key1=value1\nkey2=value2"
        result = await DahuaClient.parse_dahua_api_response(data)
        assert result == {"key1": "value1", "key2": "value2"}

    @pytest.mark.asyncio
    async def test_single_value(self):
        data = "OK"
        result = await DahuaClient.parse_dahua_api_response(data)
        assert result == {"OK": "OK"}

    @pytest.mark.asyncio
    async def test_equals_in_value(self):
        data = "key=value=with=equals"
        result = await DahuaClient.parse_dahua_api_response(data)
        assert result == {"key": "value=with=equals"}

    @pytest.mark.asyncio
    async def test_empty_string(self):
        result = await DahuaClient.parse_dahua_api_response("")
        assert result == {}


# --- get() method ---


class TestGet:
    @pytest.mark.asyncio
    async def test_success(self):
        session = MagicMock()
        client = DahuaClient("admin", "pass", "192.168.1.1", 80, 554, session)

        mock_response = AsyncMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.text = AsyncMock(return_value="key=value")
        mock_response.close = MagicMock()

        with patch("custom_components.dahua.client.DigestAuth") as mock_auth_cls:
            mock_auth = mock_auth_cls.return_value
            mock_auth.request = AsyncMock(return_value=mock_response)

            result = await client.get("/cgi-bin/test")
            assert result == {"key": "value"}
            mock_response.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_verify_ok_success(self):
        session = MagicMock()
        client = DahuaClient("admin", "pass", "192.168.1.1", 80, 554, session)

        mock_response = AsyncMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.text = AsyncMock(return_value="OK")
        mock_response.close = MagicMock()

        with patch("custom_components.dahua.client.DigestAuth") as mock_auth_cls:
            mock_auth = mock_auth_cls.return_value
            mock_auth.request = AsyncMock(return_value=mock_response)

            result = await client.get("/cgi-bin/test", verify_ok=True)
            assert "OK" in result

    @pytest.mark.asyncio
    async def test_verify_ok_failure(self):
        session = MagicMock()
        client = DahuaClient("admin", "pass", "192.168.1.1", 80, 554, session)

        mock_response = AsyncMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.text = AsyncMock(return_value="Error")
        mock_response.close = MagicMock()

        with patch("custom_components.dahua.client.DigestAuth") as mock_auth_cls:
            mock_auth = mock_auth_cls.return_value
            mock_auth.request = AsyncMock(return_value=mock_response)

            with pytest.raises(Exception, match="Error"):
                await client.get("/cgi-bin/test", verify_ok=True)

    @pytest.mark.asyncio
    async def test_timeout(self):
        import asyncio

        session = MagicMock()
        client = DahuaClient("admin", "pass", "192.168.1.1", 80, 554, session)

        with patch("custom_components.dahua.client.DigestAuth") as mock_auth_cls:
            mock_auth = mock_auth_cls.return_value
            mock_auth.request = AsyncMock(side_effect=asyncio.TimeoutError())

            with pytest.raises(asyncio.TimeoutError):
                await client.get("/cgi-bin/test")

    @pytest.mark.asyncio
    async def test_client_error(self):
        session = MagicMock()
        client = DahuaClient("admin", "pass", "192.168.1.1", 80, 554, session)

        with patch("custom_components.dahua.client.DigestAuth") as mock_auth_cls:
            mock_auth = mock_auth_cls.return_value
            mock_auth.request = AsyncMock(side_effect=aiohttp.ClientError("fail"))

            with pytest.raises(aiohttp.ClientError):
                await client.get("/cgi-bin/test")


# --- get_bytes() ---


class TestGetBytes:
    @pytest.mark.asyncio
    async def test_returns_bytes(self):
        session = MagicMock()
        client = DahuaClient("admin", "pass", "192.168.1.1", 80, 554, session)

        mock_response = AsyncMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.read = AsyncMock(return_value=b"\xff\xd8\xff\xe0")
        mock_response.close = MagicMock()

        with patch("custom_components.dahua.client.DigestAuth") as mock_auth_cls:
            mock_auth = mock_auth_cls.return_value
            mock_auth.request = AsyncMock(return_value=mock_response)

            result = await client.get_bytes("/cgi-bin/snapshot.cgi?channel=1")
            assert result == b"\xff\xd8\xff\xe0"
            mock_response.close.assert_called_once()


# --- Representative API methods ---


class TestAsyncGetSystemInfo:
    @pytest.mark.asyncio
    async def test_success(self):
        session = MagicMock()
        client = DahuaClient("admin", "pass", "192.168.1.1", 80, 554, session)

        mock_response = AsyncMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.text = AsyncMock(
            return_value="deviceType=IPC-HDW5831R-ZE\nserialNumber=ABC123"
        )
        mock_response.close = MagicMock()

        with patch("custom_components.dahua.client.DigestAuth") as mock_auth_cls:
            mock_auth = mock_auth_cls.return_value
            mock_auth.request = AsyncMock(return_value=mock_response)

            result = await client.async_get_system_info()
            assert result["deviceType"] == "IPC-HDW5831R-ZE"
            assert result["serialNumber"] == "ABC123"

    @pytest.mark.asyncio
    async def test_fallback_on_error(self):
        session = MagicMock()
        client = DahuaClient("admin", "pass", "192.168.1.1", 80, 554, session)

        req_info = MagicMock()
        req_info.real_url = "http://test"

        with patch("custom_components.dahua.client.DigestAuth") as mock_auth_cls:
            mock_auth = mock_auth_cls.return_value
            mock_auth.request = AsyncMock(
                side_effect=aiohttp.ClientResponseError(req_info, ())
            )

            result = await client.async_get_system_info()
            assert "serialNumber" in result


class TestGetDeviceType:
    @pytest.mark.asyncio
    async def test_success(self):
        session = MagicMock()
        client = DahuaClient("admin", "pass", "192.168.1.1", 80, 554, session)

        mock_response = AsyncMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.text = AsyncMock(return_value="type=IPC-HDW5831R-ZE")
        mock_response.close = MagicMock()

        with patch("custom_components.dahua.client.DigestAuth") as mock_auth_cls:
            mock_auth = mock_auth_cls.return_value
            mock_auth.request = AsyncMock(return_value=mock_response)

            result = await client.get_device_type()
            assert result["type"] == "IPC-HDW5831R-ZE"

    @pytest.mark.asyncio
    async def test_fallback(self):
        session = MagicMock()
        client = DahuaClient("admin", "pass", "192.168.1.1", 80, 554, session)

        req_info = MagicMock()
        req_info.real_url = "http://test"

        with patch("custom_components.dahua.client.DigestAuth") as mock_auth_cls:
            mock_auth = mock_auth_cls.return_value
            mock_auth.request = AsyncMock(
                side_effect=aiohttp.ClientResponseError(req_info, ())
            )

            result = await client.get_device_type()
            assert result["type"] == "Generic RTSP"


class TestGetMaxExtraStreams:
    @pytest.mark.asyncio
    async def test_success(self):
        session = MagicMock()
        client = DahuaClient("admin", "pass", "192.168.1.1", 80, 554, session)

        mock_response = AsyncMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.text = AsyncMock(return_value="table.MaxExtraStream=2")
        mock_response.close = MagicMock()

        with patch("custom_components.dahua.client.DigestAuth") as mock_auth_cls:
            mock_auth = mock_auth_cls.return_value
            mock_auth.request = AsyncMock(return_value=mock_response)

            result = await client.get_max_extra_streams()
            assert result == 2

    @pytest.mark.asyncio
    async def test_fallback_returns_3(self):
        session = MagicMock()
        client = DahuaClient("admin", "pass", "192.168.1.1", 80, 554, session)

        req_info = MagicMock()
        req_info.real_url = "http://test"

        with patch("custom_components.dahua.client.DigestAuth") as mock_auth_cls:
            mock_auth = mock_auth_cls.return_value
            mock_auth.request = AsyncMock(
                side_effect=aiohttp.ClientResponseError(req_info, ())
            )

            result = await client.get_max_extra_streams()
            assert result == 3


class TestEnableMotionDetection:
    @pytest.mark.asyncio
    async def test_ok_response(self):
        session = MagicMock()
        client = DahuaClient("admin", "pass", "192.168.1.1", 80, 554, session)

        mock_response = AsyncMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.text = AsyncMock(return_value="OK")
        mock_response.close = MagicMock()

        with patch("custom_components.dahua.client.DigestAuth") as mock_auth_cls:
            mock_auth = mock_auth_cls.return_value
            mock_auth.request = AsyncMock(return_value=mock_response)

            result = await client.enable_motion_detection(0, True)
            assert "OK" in result

    @pytest.mark.asyncio
    async def test_fallback_url(self):
        """When first API returns non-OK, it falls back to simpler URL."""
        session = MagicMock()
        client = DahuaClient("admin", "pass", "192.168.1.1", 80, 554, session)

        # First call returns non-OK, second call returns OK
        mock_resp_fail = AsyncMock()
        mock_resp_fail.raise_for_status = MagicMock()
        mock_resp_fail.text = AsyncMock(return_value="Error")
        mock_resp_fail.close = MagicMock()

        mock_resp_ok = AsyncMock()
        mock_resp_ok.raise_for_status = MagicMock()
        mock_resp_ok.text = AsyncMock(return_value="OK")
        mock_resp_ok.close = MagicMock()

        with patch("custom_components.dahua.client.DigestAuth") as mock_auth_cls:
            mock_auth = mock_auth_cls.return_value
            mock_auth.request = AsyncMock(side_effect=[mock_resp_fail, mock_resp_ok])

            await client.enable_motion_detection(0, True)
            assert mock_auth.request.call_count == 2


class TestAsyncSetLightingV1:
    @pytest.mark.asyncio
    async def test_turn_on(self):
        session = MagicMock()
        client = DahuaClient("admin", "pass", "192.168.1.1", 80, 554, session)

        mock_response = AsyncMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.text = AsyncMock(return_value="OK")
        mock_response.close = MagicMock()

        with patch("custom_components.dahua.client.DigestAuth") as mock_auth_cls:
            mock_auth = mock_auth_cls.return_value
            mock_auth.request = AsyncMock(return_value=mock_response)

            await client.async_set_lighting_v1(0, True, 80)
            url = mock_auth.request.call_args[0][1]
            assert "Mode=Manual" in url
            assert "Light=80" in url

    @pytest.mark.asyncio
    async def test_turn_off(self):
        session = MagicMock()
        client = DahuaClient("admin", "pass", "192.168.1.1", 80, 554, session)

        mock_response = AsyncMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.text = AsyncMock(return_value="OK")
        mock_response.close = MagicMock()

        with patch("custom_components.dahua.client.DigestAuth") as mock_auth_cls:
            mock_auth = mock_auth_cls.return_value
            mock_auth.request = AsyncMock(return_value=mock_response)

            await client.async_set_lighting_v1(0, False, 80)
            url = mock_auth.request.call_args[0][1]
            assert "Mode=Off" in url


class TestAsyncSetVideoProfileMode:
    @pytest.mark.asyncio
    async def test_day_mode(self):
        session = MagicMock()
        client = DahuaClient("admin", "pass", "192.168.1.1", 80, 554, session)

        mock_response = AsyncMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.text = AsyncMock(return_value="ok")
        mock_response.close = MagicMock()

        with patch("custom_components.dahua.client.DigestAuth") as mock_auth_cls:
            mock_auth = mock_auth_cls.return_value
            mock_auth.request = AsyncMock(return_value=mock_response)

            await client.async_set_video_profile_mode(0, "Day")
            url = mock_auth.request.call_args[0][1]
            assert "Config[0]=0" in url

    @pytest.mark.asyncio
    async def test_night_mode(self):
        session = MagicMock()
        client = DahuaClient("admin", "pass", "192.168.1.1", 80, 554, session)

        mock_response = AsyncMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.text = AsyncMock(return_value="ok")
        mock_response.close = MagicMock()

        with patch("custom_components.dahua.client.DigestAuth") as mock_auth_cls:
            mock_auth = mock_auth_cls.return_value
            mock_auth.request = AsyncMock(return_value=mock_response)

            await client.async_set_video_profile_mode(0, "Night")
            url = mock_auth.request.call_args[0][1]
            assert "Config[0]=1" in url


class TestAsyncSetLightingV2ForAmcrestDoorbells:
    @pytest.mark.asyncio
    async def test_on(self):
        session = MagicMock()
        client = DahuaClient("admin", "pass", "192.168.1.1", 80, 554, session)

        mock_response = AsyncMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.text = AsyncMock(return_value="OK")
        mock_response.close = MagicMock()

        with patch("custom_components.dahua.client.DigestAuth") as mock_auth_cls:
            mock_auth = mock_auth_cls.return_value
            mock_auth.request = AsyncMock(return_value=mock_response)

            await client.async_set_lighting_v2_for_amcrest_doorbells("On")
            url = mock_auth.request.call_args[0][1]
            assert "ForceOn" in url
            assert "State=On" in url

    @pytest.mark.asyncio
    async def test_strobe(self):
        session = MagicMock()
        client = DahuaClient("admin", "pass", "192.168.1.1", 80, 554, session)

        mock_response = AsyncMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.text = AsyncMock(return_value="OK")
        mock_response.close = MagicMock()

        with patch("custom_components.dahua.client.DigestAuth") as mock_auth_cls:
            mock_auth = mock_auth_cls.return_value
            mock_auth.request = AsyncMock(return_value=mock_response)

            await client.async_set_lighting_v2_for_amcrest_doorbells("Strobe")
            url = mock_auth.request.call_args[0][1]
            assert "ForceOn" in url
            assert "Flicker" in url

    @pytest.mark.asyncio
    async def test_off(self):
        session = MagicMock()
        client = DahuaClient("admin", "pass", "192.168.1.1", 80, 554, session)

        mock_response = AsyncMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.text = AsyncMock(return_value="OK")
        mock_response.close = MagicMock()

        with patch("custom_components.dahua.client.DigestAuth") as mock_auth_cls:
            mock_auth = mock_auth_cls.return_value
            mock_auth.request = AsyncMock(return_value=mock_response)

            await client.async_set_lighting_v2_for_amcrest_doorbells("Off")
            url = mock_auth.request.call_args[0][1]
            assert "Mode=Off" in url


class TestAsyncSetRecordMode:
    @pytest.mark.asyncio
    async def test_auto(self):
        session = MagicMock()
        client = DahuaClient("admin", "pass", "192.168.1.1", 80, 554, session)

        mock_response = AsyncMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.text = AsyncMock(return_value="OK")
        mock_response.close = MagicMock()

        with patch("custom_components.dahua.client.DigestAuth") as mock_auth_cls:
            mock_auth = mock_auth_cls.return_value
            mock_auth.request = AsyncMock(return_value=mock_response)

            await client.async_set_record_mode(0, "auto")
            url = mock_auth.request.call_args[0][1]
            assert "Mode=0" in url

    @pytest.mark.asyncio
    async def test_on(self):
        session = MagicMock()
        client = DahuaClient("admin", "pass", "192.168.1.1", 80, 554, session)

        mock_response = AsyncMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.text = AsyncMock(return_value="OK")
        mock_response.close = MagicMock()

        with patch("custom_components.dahua.client.DigestAuth") as mock_auth_cls:
            mock_auth = mock_auth_cls.return_value
            mock_auth.request = AsyncMock(return_value=mock_response)

            await client.async_set_record_mode(0, "on")
            url = mock_auth.request.call_args[0][1]
            assert "Mode=1" in url

    @pytest.mark.asyncio
    async def test_off(self):
        session = MagicMock()
        client = DahuaClient("admin", "pass", "192.168.1.1", 80, 554, session)

        mock_response = AsyncMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.text = AsyncMock(return_value="OK")
        mock_response.close = MagicMock()

        with patch("custom_components.dahua.client.DigestAuth") as mock_auth_cls:
            mock_auth = mock_auth_cls.return_value
            mock_auth.request = AsyncMock(return_value=mock_response)

            await client.async_set_record_mode(0, "off")
            url = mock_auth.request.call_args[0][1]
            assert "Mode=2" in url


class TestAsyncSetVideoInDayNightMode:
    @pytest.mark.asyncio
    async def test_success(self):
        session = MagicMock()
        client = DahuaClient("admin", "pass", "192.168.1.1", 80, 554, session)

        mock_response = AsyncMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.text = AsyncMock(return_value="OK")
        mock_response.close = MagicMock()

        with patch("custom_components.dahua.client.DigestAuth") as mock_auth_cls:
            mock_auth = mock_auth_cls.return_value
            mock_auth.request = AsyncMock(return_value=mock_response)

            await client.async_set_video_in_day_night_mode(0, "day", "color")
            url = mock_auth.request.call_args[0][1]
            assert "VideoInDayNight[0][0]" in url
            assert "Mode=Color" in url

    @pytest.mark.asyncio
    async def test_failure(self):
        session = MagicMock()
        client = DahuaClient("admin", "pass", "192.168.1.1", 80, 554, session)

        mock_response = AsyncMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.text = AsyncMock(return_value="Error")
        mock_response.close = MagicMock()

        with patch("custom_components.dahua.client.DigestAuth") as mock_auth_cls:
            mock_auth = mock_auth_cls.return_value
            mock_auth.request = AsyncMock(return_value=mock_response)

            with pytest.raises(Exception, match="Could not set Day/Night mode"):
                await client.async_set_video_in_day_night_mode(0, "general", "auto")


class TestAsyncSetEventNotifications:
    @pytest.mark.asyncio
    async def test_enable_sets_false(self):
        """Enabling event notifications sets DisableEventNotify.Enable=false (inverted logic)."""
        session = MagicMock()
        client = DahuaClient("admin", "pass", "192.168.1.1", 80, 554, session)

        mock_response = AsyncMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.text = AsyncMock(return_value="OK")
        mock_response.close = MagicMock()

        with patch("custom_components.dahua.client.DigestAuth") as mock_auth_cls:
            mock_auth = mock_auth_cls.return_value
            mock_auth.request = AsyncMock(return_value=mock_response)

            await client.async_set_event_notifications(0, True)
            url = mock_auth.request.call_args[0][1]
            assert "Enable=false" in url

    @pytest.mark.asyncio
    async def test_disable_sets_true(self):
        """Disabling event notifications sets DisableEventNotify.Enable=true (inverted logic)."""
        session = MagicMock()
        client = DahuaClient("admin", "pass", "192.168.1.1", 80, 554, session)

        mock_response = AsyncMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.text = AsyncMock(return_value="OK")
        mock_response.close = MagicMock()

        with patch("custom_components.dahua.client.DigestAuth") as mock_auth_cls:
            mock_auth = mock_auth_cls.return_value
            mock_auth.request = AsyncMock(return_value=mock_response)

            await client.async_set_event_notifications(0, False)
            url = mock_auth.request.call_args[0][1]
            assert "Enable=true" in url


class TestStreamEvents:
    @pytest.mark.asyncio
    async def test_stream_events(self):
        """Test stream_events connects and passes data to callback."""
        session = MagicMock()
        client = DahuaClient("admin", "pass", "192.168.1.1", 80, 554, session)

        received = []

        def on_receive(data, channel):
            received.append((data, channel))

        # Create a mock response with async iterator
        mock_response = AsyncMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.close = MagicMock()

        async def mock_iter_chunks():
            yield (b"Code=VideoMotion;action=Start;index=0", True)

        mock_response.content = MagicMock()
        mock_response.content.iter_chunks = mock_iter_chunks

        with patch("custom_components.dahua.client.DigestAuth") as mock_auth_cls:
            mock_auth = mock_auth_cls.return_value
            mock_auth.request = AsyncMock(return_value=mock_response)

            await client.stream_events(on_receive, ["VideoMotion"], 0)

        assert len(received) == 1
        assert received[0][1] == 0


# --- Remaining API method tests (using patch.object on get/get_bytes) ---


def _make_client():
    """Helper to create a DahuaClient with a mock session."""
    return DahuaClient("admin", "pass", "192.168.1.1", 80, 554, MagicMock())


class TestAsyncGetSnapshot:
    @pytest.mark.asyncio
    async def test_returns_bytes(self):
        client = _make_client()
        with patch.object(
            client, "get_bytes", new_callable=AsyncMock, return_value=b"\xff\xd8"
        ) as mock_get:
            result = await client.async_get_snapshot(1)
            assert result == b"\xff\xd8"
            assert "snapshot.cgi?channel=1" in mock_get.call_args[0][0]


class TestGetSoftwareVersion:
    @pytest.mark.asyncio
    async def test_success(self):
        client = _make_client()
        with patch.object(
            client,
            "get",
            new_callable=AsyncMock,
            return_value={"version": "2.800"},
        ):
            result = await client.get_software_version()
            assert result["version"] == "2.800"

    @pytest.mark.asyncio
    async def test_fallback(self):
        client = _make_client()
        req_info = MagicMock()
        req_info.real_url = "http://test"
        with patch.object(
            client,
            "get",
            new_callable=AsyncMock,
            side_effect=aiohttp.ClientResponseError(req_info, ()),
        ):
            result = await client.get_software_version()
            assert result["version"] == "1.0"


class TestGetMachineName:
    @pytest.mark.asyncio
    async def test_success(self):
        client = _make_client()
        with patch.object(
            client,
            "get",
            new_callable=AsyncMock,
            return_value={"name": "FrontDoor"},
        ):
            result = await client.get_machine_name()
            assert result["name"] == "FrontDoor"

    @pytest.mark.asyncio
    async def test_fallback(self):
        client = _make_client()
        req_info = MagicMock()
        req_info.real_url = "http://test"
        with patch.object(
            client,
            "get",
            new_callable=AsyncMock,
            side_effect=aiohttp.ClientResponseError(req_info, ()),
        ):
            result = await client.get_machine_name()
            assert "name" in result


class TestGetVendor:
    @pytest.mark.asyncio
    async def test_success(self):
        client = _make_client()
        with patch.object(
            client,
            "get",
            new_callable=AsyncMock,
            return_value={"vendor": "Dahua"},
        ):
            result = await client.get_vendor()
            assert result["vendor"] == "Dahua"

    @pytest.mark.asyncio
    async def test_fallback(self):
        client = _make_client()
        req_info = MagicMock()
        req_info.real_url = "http://test"
        with patch.object(
            client,
            "get",
            new_callable=AsyncMock,
            side_effect=aiohttp.ClientResponseError(req_info, ()),
        ):
            result = await client.get_vendor()
            assert result["vendor"] == "Generic RTSP"


class TestReboot:
    @pytest.mark.asyncio
    async def test_calls_get(self):
        client = _make_client()
        with patch.object(
            client, "get", new_callable=AsyncMock, return_value={"OK": "OK"}
        ) as mock_get:
            await client.reboot()
            assert "reboot" in mock_get.call_args[0][0]


class TestAsyncGetCoaxialControlIOStatus:
    @pytest.mark.asyncio
    async def test_calls_get(self):
        client = _make_client()
        with patch.object(
            client,
            "get",
            new_callable=AsyncMock,
            return_value={"status.status.Speaker": "Off"},
        ) as mock_get:
            result = await client.async_get_coaxial_control_io_status()
            assert "coaxialControlIO" in mock_get.call_args[0][0]
            assert result["status.status.Speaker"] == "Off"


class TestAsyncGetLightingV2:
    @pytest.mark.asyncio
    async def test_calls_get(self):
        client = _make_client()
        with patch.object(
            client,
            "get",
            new_callable=AsyncMock,
            return_value={"table.Lighting_V2[0][0][0].Mode": "Off"},
        ):
            result = await client.async_get_lighting_v2()
            assert "Mode" in list(result.keys())[0]


class TestAsyncGetMachineName:
    @pytest.mark.asyncio
    async def test_success(self):
        client = _make_client()
        with patch.object(
            client,
            "get",
            new_callable=AsyncMock,
            return_value={"table.General.MachineName": "Cam4"},
        ):
            result = await client.async_get_machine_name()
            assert result["table.General.MachineName"] == "Cam4"

    @pytest.mark.asyncio
    async def test_fallback(self):
        client = _make_client()
        req_info = MagicMock()
        req_info.real_url = "http://test"
        with patch.object(
            client,
            "get",
            new_callable=AsyncMock,
            side_effect=aiohttp.ClientResponseError(req_info, ()),
        ):
            result = await client.async_get_machine_name()
            assert "table.General.MachineName" in result


class TestAsyncGetConfig:
    @pytest.mark.asyncio
    async def test_success(self):
        client = _make_client()
        with patch.object(
            client,
            "get",
            new_callable=AsyncMock,
            return_value={"table.Lighting[0][0].Mode": "Auto"},
        ):
            result = await client.async_get_config("Lighting[0][0]")
            assert result["table.Lighting[0][0].Mode"] == "Auto"

    @pytest.mark.asyncio
    async def test_fallback(self):
        client = _make_client()
        req_info = MagicMock()
        req_info.real_url = "http://test"
        with patch.object(
            client,
            "get",
            new_callable=AsyncMock,
            side_effect=aiohttp.ClientResponseError(req_info, ()),
        ):
            result = await client.async_get_config("Lighting[0][0]")
            assert result == {}


class TestAsyncGetConfigLighting:
    @pytest.mark.asyncio
    async def test_success(self):
        client = _make_client()
        with patch.object(
            client,
            "async_get_config",
            new_callable=AsyncMock,
            return_value={"table.Lighting[0][0].Mode": "Auto"},
        ):
            result = await client.async_get_config_lighting(0, 0)
            assert "Mode" in list(result.keys())[0]

    @pytest.mark.asyncio
    async def test_400_error_returns_empty(self):
        client = _make_client()
        req_info = MagicMock()
        req_info.real_url = "http://test"
        with patch.object(
            client,
            "async_get_config",
            new_callable=AsyncMock,
            side_effect=aiohttp.ClientResponseError(req_info, (), status=400),
        ):
            result = await client.async_get_config_lighting(0, 0)
            assert result == {}

    @pytest.mark.asyncio
    async def test_non_400_error_raises(self):
        client = _make_client()
        req_info = MagicMock()
        req_info.real_url = "http://test"
        with patch.object(
            client,
            "async_get_config",
            new_callable=AsyncMock,
            side_effect=aiohttp.ClientResponseError(req_info, (), status=500),
        ):
            with pytest.raises(aiohttp.ClientResponseError):
                await client.async_get_config_lighting(0, 0)


class TestAsyncGetConfigMotionDetection:
    @pytest.mark.asyncio
    async def test_fallback(self):
        client = _make_client()
        req_info = MagicMock()
        req_info.real_url = "http://test"
        with patch.object(
            client,
            "async_get_config",
            new_callable=AsyncMock,
            side_effect=aiohttp.ClientResponseError(req_info, ()),
        ):
            result = await client.async_get_config_motion_detection()
            assert result["table.MotionDetect[0].Enable"] == "false"


class TestAsyncGetVideoAnalyseRulesForAmcrest:
    @pytest.mark.asyncio
    async def test_success(self):
        client = _make_client()
        with patch.object(
            client,
            "async_get_config",
            new_callable=AsyncMock,
            return_value={"table.VideoAnalyseRule[0][0].Enable": "true"},
        ):
            result = await client.async_get_video_analyse_rules_for_amcrest()
            assert result["table.VideoAnalyseRule[0][0].Enable"] == "true"

    @pytest.mark.asyncio
    async def test_fallback(self):
        client = _make_client()
        req_info = MagicMock()
        req_info.real_url = "http://test"
        with patch.object(
            client,
            "async_get_config",
            new_callable=AsyncMock,
            side_effect=aiohttp.ClientResponseError(req_info, ()),
        ):
            result = await client.async_get_video_analyse_rules_for_amcrest()
            assert result["table.VideoAnalyseRule[0][0].Enable"] == "false"


class TestAsyncGetIvsRules:
    @pytest.mark.asyncio
    async def test_calls_get_config(self):
        client = _make_client()
        with patch.object(
            client,
            "async_get_config",
            new_callable=AsyncMock,
            return_value={"table.VideoAnalyseRule[0][0].Enable": "true"},
        ) as mock_cfg:
            await client.async_get_ivs_rules()
            mock_cfg.assert_called_once_with("VideoAnalyseRule")


class TestAsyncSetAllIvsRules:
    @pytest.mark.asyncio
    async def test_sets_matching_rules(self):
        client = _make_client()
        rules = {
            "table.VideoAnalyseRule[0][0].Enable": "true",
            "table.VideoAnalyseRule[0][1].Enable": "false",
            "table.VideoAnalyseRule[0][0].Name": "IVS-0",
        }
        with patch.object(
            client,
            "async_get_ivs_rules",
            new_callable=AsyncMock,
            return_value=rules,
        ):
            with patch.object(
                client, "get", new_callable=AsyncMock, return_value={"OK": "OK"}
            ) as mock_get:
                await client.async_set_all_ivs_rules(0, True)
                url = mock_get.call_args[0][0]
                assert "VideoAnalyseRule[0][0].Enable=true" in url
                assert "VideoAnalyseRule[0][1].Enable=true" in url

    @pytest.mark.asyncio
    async def test_no_matching_rules(self):
        client = _make_client()
        with patch.object(
            client,
            "async_get_ivs_rules",
            new_callable=AsyncMock,
            return_value={},
        ):
            with patch.object(client, "get", new_callable=AsyncMock) as mock_get:
                await client.async_set_all_ivs_rules(0, True)
                mock_get.assert_not_called()


class TestAsyncSetIvsRule:
    @pytest.mark.asyncio
    async def test_calls_get(self):
        client = _make_client()
        with patch.object(
            client, "get", new_callable=AsyncMock, return_value={"OK": "OK"}
        ) as mock_get:
            await client.async_set_ivs_rule(0, 1, True)
            url = mock_get.call_args[0][0]
            assert "VideoAnalyseRule[0][1].Enable=true" in url


class TestAsyncEnabledSmartMotionDetection:
    @pytest.mark.asyncio
    async def test_enable(self):
        client = _make_client()
        with patch.object(
            client, "get", new_callable=AsyncMock, return_value={"OK": "OK"}
        ) as mock_get:
            await client.async_enabled_smart_motion_detection(True)
            url = mock_get.call_args[0][0]
            assert "SmartMotionDetect[0].Enable=true" in url


class TestAsyncSetLightGlobalEnabled:
    @pytest.mark.asyncio
    async def test_enable(self):
        client = _make_client()
        with patch.object(
            client, "get", new_callable=AsyncMock, return_value={"OK": "OK"}
        ) as mock_get:
            await client.async_set_light_global_enabled(True)
            url = mock_get.call_args[0][0]
            assert "LightGlobal[0].Enable=true" in url


class TestAsyncGetSmartMotionDetection:
    @pytest.mark.asyncio
    async def test_calls_get(self):
        client = _make_client()
        with patch.object(
            client,
            "get",
            new_callable=AsyncMock,
            return_value={"table.SmartMotionDetect[0].Enable": "true"},
        ):
            result = await client.async_get_smart_motion_detection()
            assert result["table.SmartMotionDetect[0].Enable"] == "true"


class TestAsyncGetPtzPosition:
    @pytest.mark.asyncio
    async def test_calls_get(self):
        client = _make_client()
        with patch.object(
            client,
            "get",
            new_callable=AsyncMock,
            return_value={"status.PresetID": "2"},
        ):
            result = await client.async_get_ptz_position()
            assert result["status.PresetID"] == "2"


class TestAsyncGetLightGlobalEnabled:
    @pytest.mark.asyncio
    async def test_calls_get(self):
        client = _make_client()
        with patch.object(
            client,
            "get",
            new_callable=AsyncMock,
            return_value={"table.LightGlobal[0].Enable": "true"},
        ):
            result = await client.async_get_light_global_enabled()
            assert result["table.LightGlobal[0].Enable"] == "true"


class TestAsyncGetFloodlightmode:
    @pytest.mark.asyncio
    async def test_success(self):
        client = _make_client()
        with patch.object(
            client,
            "async_get_config",
            new_callable=AsyncMock,
            return_value={"table.FloodLightMode.Mode": "2"},
        ):
            result = await client.async_get_floodlightmode()
            assert "table.FloodLightMode.Mode" in result

    @pytest.mark.asyncio
    async def test_fallback(self):
        client = _make_client()
        req_info = MagicMock()
        req_info.real_url = "http://test"
        with patch.object(
            client,
            "async_get_config",
            new_callable=AsyncMock,
            side_effect=aiohttp.ClientResponseError(req_info, ()),
        ):
            result = await client.async_get_floodlightmode()
            assert result == 2


class TestAsyncSetFloodlightmode:
    @pytest.mark.asyncio
    async def test_calls_get(self):
        client = _make_client()
        with patch.object(
            client, "get", new_callable=AsyncMock, return_value={"OK": "OK"}
        ) as mock_get:
            await client.async_set_floodlightmode(2)
            url = mock_get.call_args[0][0]
            assert "FloodLightMode.Mode=2" in url


class TestAsyncSetLightingV1Mode:
    @pytest.mark.asyncio
    async def test_on_converted_to_manual(self):
        client = _make_client()
        with patch.object(
            client, "get", new_callable=AsyncMock, return_value={"OK": "OK"}
        ) as mock_get:
            await client.async_set_lighting_v1_mode(0, "on", 50)
            url = mock_get.call_args[0][0]
            assert "Mode=Manual" in url
            assert "Light=50" in url


class TestAsyncGotoPresetPosition:
    @pytest.mark.asyncio
    async def test_calls_get(self):
        client = _make_client()
        with patch.object(
            client, "get", new_callable=AsyncMock, return_value={"OK": "OK"}
        ) as mock_get:
            await client.async_goto_preset_position(0, 3)
            url = mock_get.call_args[0][0]
            assert "GotoPreset" in url
            assert "arg2=3" in url


class TestAsyncAdjustfocusV1:
    @pytest.mark.asyncio
    async def test_calls_get(self):
        client = _make_client()
        with patch.object(
            client, "get", new_callable=AsyncMock, return_value={"OK": "OK"}
        ) as mock_get:
            await client.async_adjustfocus_v1("10", "5")
            url = mock_get.call_args[0][0]
            assert "focus=10" in url
            assert "zoom=5" in url


class TestAsyncSetPrivacyMask:
    @pytest.mark.asyncio
    async def test_calls_get(self):
        client = _make_client()
        with patch.object(
            client, "get", new_callable=AsyncMock, return_value={"OK": "OK"}
        ) as mock_get:
            await client.async_setprivacymask(0, True)
            url = mock_get.call_args[0][0]
            assert "PrivacyMasking[0][0].Enable=true" in url


class TestAsyncSetNightSwitchMode:
    @pytest.mark.asyncio
    async def test_night(self):
        client = _make_client()
        with patch.object(
            client, "get", new_callable=AsyncMock, return_value={"OK": "OK"}
        ) as mock_get:
            await client.async_set_night_switch_mode(0, "Night")
            url = mock_get.call_args[0][0]
            assert "SwitchMode=3" in url

    @pytest.mark.asyncio
    async def test_day(self):
        client = _make_client()
        with patch.object(
            client, "get", new_callable=AsyncMock, return_value={"OK": "OK"}
        ) as mock_get:
            await client.async_set_night_switch_mode(0, "Day")
            url = mock_get.call_args[0][0]
            assert "SwitchMode=0" in url


class TestAsyncEnableChannelTitle:
    @pytest.mark.asyncio
    async def test_success(self):
        client = _make_client()
        with patch.object(
            client,
            "get",
            new_callable=AsyncMock,
            return_value={"OK": "OK"},
        ) as mock_get:
            await client.async_enable_channel_title(0, True)
            url = mock_get.call_args[0][0]
            assert "ChannelTitle.EncodeBlend=true" in url

    @pytest.mark.asyncio
    async def test_failure_raises(self):
        client = _make_client()
        with patch.object(
            client,
            "get",
            new_callable=AsyncMock,
            return_value={"Error": "Error"},
        ):
            with pytest.raises(Exception, match="enable/disable channel title"):
                await client.async_enable_channel_title(0, True)


class TestAsyncEnableTimeOverlay:
    @pytest.mark.asyncio
    async def test_success(self):
        client = _make_client()
        with patch.object(
            client,
            "get",
            new_callable=AsyncMock,
            return_value={"OK": "OK"},
        ) as mock_get:
            await client.async_enable_time_overlay(0, True)
            url = mock_get.call_args[0][0]
            assert "TimeTitle.EncodeBlend=true" in url

    @pytest.mark.asyncio
    async def test_failure_raises(self):
        client = _make_client()
        with patch.object(
            client,
            "get",
            new_callable=AsyncMock,
            return_value={"Error": "Error"},
        ):
            with pytest.raises(Exception, match="enable/disable time overlay"):
                await client.async_enable_time_overlay(0, True)


class TestAsyncEnableTextOverlay:
    @pytest.mark.asyncio
    async def test_success(self):
        client = _make_client()
        with patch.object(
            client,
            "get",
            new_callable=AsyncMock,
            return_value={"OK": "OK"},
        ) as mock_get:
            await client.async_enable_text_overlay(0, 1, True)
            url = mock_get.call_args[0][0]
            assert "CustomTitle[1].EncodeBlend=true" in url

    @pytest.mark.asyncio
    async def test_failure_raises(self):
        client = _make_client()
        with patch.object(
            client,
            "get",
            new_callable=AsyncMock,
            return_value={"Error": "Error"},
        ):
            with pytest.raises(Exception, match="enable/disable text overlay"):
                await client.async_enable_text_overlay(0, 1, True)


class TestAsyncEnableCustomOverlay:
    @pytest.mark.asyncio
    async def test_success(self):
        client = _make_client()
        with patch.object(
            client,
            "get",
            new_callable=AsyncMock,
            return_value={"OK": "OK"},
        ) as mock_get:
            await client.async_enable_custom_overlay(0, 0, True)
            url = mock_get.call_args[0][0]
            assert "UserDefinedTitle[0].EncodeBlend=true" in url

    @pytest.mark.asyncio
    async def test_failure_raises(self):
        client = _make_client()
        with patch.object(
            client,
            "get",
            new_callable=AsyncMock,
            return_value={"Error": "Error"},
        ):
            with pytest.raises(Exception, match="enable/disable customer overlay"):
                await client.async_enable_custom_overlay(0, 0, True)


class TestAsyncSetServiceSetChannelTitle:
    @pytest.mark.asyncio
    async def test_success(self):
        client = _make_client()
        with patch.object(
            client,
            "get",
            new_callable=AsyncMock,
            return_value={"OK": "OK"},
        ) as mock_get:
            await client.async_set_service_set_channel_title(0, "Title1", "Title2")
            url = mock_get.call_args[0][0]
            assert "ChannelTitle[0].Name=Title1|Title2" in url

    @pytest.mark.asyncio
    async def test_failure_raises(self):
        client = _make_client()
        with patch.object(
            client,
            "get",
            new_callable=AsyncMock,
            return_value={"Error": "Error"},
        ):
            with pytest.raises(Exception, match="Could not set text"):
                await client.async_set_service_set_channel_title(0, "T1", "T2")


class TestAsyncSetServiceSetTextOverlay:
    @pytest.mark.asyncio
    async def test_success(self):
        client = _make_client()
        with patch.object(
            client,
            "get",
            new_callable=AsyncMock,
            return_value={"OK": "OK"},
        ) as mock_get:
            await client.async_set_service_set_text_overlay(0, 1, "a", "b", "c", "d")
            url = mock_get.call_args[0][0]
            assert "CustomTitle[1].Text=a|b|c|d" in url

    @pytest.mark.asyncio
    async def test_failure_raises(self):
        client = _make_client()
        with patch.object(
            client,
            "get",
            new_callable=AsyncMock,
            return_value={"Error": "Error"},
        ):
            with pytest.raises(Exception, match="Could not set text"):
                await client.async_set_service_set_text_overlay(
                    0, 0, "a", "b", "c", "d"
                )


class TestAsyncSetServiceSetCustomOverlay:
    @pytest.mark.asyncio
    async def test_success(self):
        client = _make_client()
        with patch.object(
            client,
            "get",
            new_callable=AsyncMock,
            return_value={"OK": "OK"},
        ) as mock_get:
            await client.async_set_service_set_custom_overlay(0, 0, "x", "y")
            url = mock_get.call_args[0][0]
            assert "UserDefinedTitle[0].Text=x|y" in url

    @pytest.mark.asyncio
    async def test_failure_raises(self):
        client = _make_client()
        with patch.object(
            client,
            "get",
            new_callable=AsyncMock,
            return_value={"Error": "Error"},
        ):
            with pytest.raises(Exception, match="Could not set text"):
                await client.async_set_service_set_custom_overlay(0, 0, "x", "y")


class TestAsyncSetLightingV2:
    @pytest.mark.asyncio
    async def test_turn_on(self):
        client = _make_client()
        with patch.object(
            client, "get", new_callable=AsyncMock, return_value={"OK": "OK"}
        ) as mock_get:
            await client.async_set_lighting_v2(0, True, 80, "0")
            url = mock_get.call_args[0][0]
            assert "Mode=Manual" in url
            assert "Light=80" in url

    @pytest.mark.asyncio
    async def test_turn_off(self):
        client = _make_client()
        with patch.object(
            client, "get", new_callable=AsyncMock, return_value={"OK": "OK"}
        ) as mock_get:
            await client.async_set_lighting_v2(0, False, 0, "0")
            url = mock_get.call_args[0][0]
            assert "Mode=Off" in url


class TestAsyncSetLightingV2ForFloodLights:
    @pytest.mark.asyncio
    async def test_turn_on(self):
        client = _make_client()
        with patch.object(
            client, "get", new_callable=AsyncMock, return_value={"OK": "OK"}
        ) as mock_get:
            await client.async_set_lighting_v2_for_flood_lights(0, True, "0")
            url = mock_get.call_args[0][0]
            assert "Mode=Manual" in url

    @pytest.mark.asyncio
    async def test_turn_off(self):
        client = _make_client()
        with patch.object(
            client, "get", new_callable=AsyncMock, return_value={"OK": "OK"}
        ) as mock_get:
            await client.async_set_lighting_v2_for_flood_lights(0, False, "0")
            url = mock_get.call_args[0][0]
            assert "Mode=Off" in url


class TestAsyncSetVideoInDayNightModeExtended:
    @pytest.mark.asyncio
    async def test_night_blackwhite(self):
        client = _make_client()
        with patch.object(
            client,
            "get",
            new_callable=AsyncMock,
            return_value={"OK": "OK"},
        ) as mock_get:
            await client.async_set_video_in_day_night_mode(0, "night", "blackwhite")
            url = mock_get.call_args[0][0]
            assert "VideoInDayNight[0][1]" in url
            assert "Mode=BlackWhite" in url

    @pytest.mark.asyncio
    async def test_auto_mode(self):
        client = _make_client()
        with patch.object(
            client,
            "get",
            new_callable=AsyncMock,
            return_value={"OK": "OK"},
        ) as mock_get:
            await client.async_set_video_in_day_night_mode(0, "day", None)
            url = mock_get.call_args[0][0]
            assert "Mode=Brightness" in url


class TestAsyncGetVideoInMode:
    @pytest.mark.asyncio
    async def test_calls_get(self):
        client = _make_client()
        with patch.object(
            client,
            "get",
            new_callable=AsyncMock,
            return_value={"table.VideoInMode[0].Config[0]": "2"},
        ):
            result = await client.async_get_video_in_mode()
            assert result["table.VideoInMode[0].Config[0]"] == "2"


class TestAsyncSetCoaxialControlState:
    @pytest.mark.asyncio
    async def test_turn_on(self):
        client = _make_client()
        with patch.object(
            client, "get", new_callable=AsyncMock, return_value={"OK": "OK"}
        ) as mock_get:
            await client.async_set_coaxial_control_state(0, 1, True)
            url = mock_get.call_args[0][0]
            assert "Type=1" in url
            assert "IO=1" in url

    @pytest.mark.asyncio
    async def test_turn_off(self):
        client = _make_client()
        with patch.object(
            client, "get", new_callable=AsyncMock, return_value={"OK": "OK"}
        ) as mock_get:
            await client.async_set_coaxial_control_state(0, 1, False)
            url = mock_get.call_args[0][0]
            assert "IO=2" in url


class TestAsyncSetDisarmingLinkage:
    @pytest.mark.asyncio
    async def test_enable(self):
        client = _make_client()
        with patch.object(
            client, "get", new_callable=AsyncMock, return_value={"OK": "OK"}
        ) as mock_get:
            await client.async_set_disarming_linkage(0, True)
            url = mock_get.call_args[0][0]
            assert "DisableLinkage[0].Enable=true" in url

    @pytest.mark.asyncio
    async def test_disable(self):
        client = _make_client()
        with patch.object(
            client, "get", new_callable=AsyncMock, return_value={"OK": "OK"}
        ) as mock_get:
            await client.async_set_disarming_linkage(0, False)
            url = mock_get.call_args[0][0]
            assert "DisableLinkage[0].Enable=false" in url


class TestAsyncGetDisarmingLinkage:
    @pytest.mark.asyncio
    async def test_calls_get(self):
        client = _make_client()
        with patch.object(
            client,
            "get",
            new_callable=AsyncMock,
            return_value={"table.DisableLinkage.Enable": "false"},
        ):
            result = await client.async_get_disarming_linkage()
            assert "DisableLinkage" in list(result.keys())[0]


class TestAsyncGetEventNotifications:
    @pytest.mark.asyncio
    async def test_calls_get(self):
        client = _make_client()
        with patch.object(
            client,
            "get",
            new_callable=AsyncMock,
            return_value={"table.DisableEventNotify.Enable": "false"},
        ):
            result = await client.async_get_event_notifications()
            assert "DisableEventNotify" in list(result.keys())[0]


class TestAsyncAccessControlOpenDoor:
    @pytest.mark.asyncio
    async def test_calls_get(self):
        client = _make_client()
        with patch.object(
            client, "get", new_callable=AsyncMock, return_value={"OK": "OK"}
        ) as mock_get:
            await client.async_access_control_open_door(1)
            url = mock_get.call_args[0][0]
            assert "accessControl" in url
            assert "channel=1" in url


class TestGetKeyError:
    @pytest.mark.asyncio
    async def test_key_error_raised(self):
        """KeyError during get is re-raised."""
        session = MagicMock()
        client = DahuaClient("admin", "pass", "192.168.1.1", 80, 554, session)

        with patch("custom_components.dahua.client.DigestAuth") as mock_auth_cls:
            mock_auth = mock_auth_cls.return_value
            mock_auth.request = AsyncMock(side_effect=KeyError("bad"))

            with pytest.raises(KeyError):
                await client.get("/cgi-bin/test")
