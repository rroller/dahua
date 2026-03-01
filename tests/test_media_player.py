"""Tests for media_player platform."""

from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from homeassistant.components.media_player import (
    MediaPlayerEntityFeature,
    MediaPlayerState,
)

from custom_components.dahua.client import _parse_adts_frames
from custom_components.dahua.media_player import (
    DahuaSpeaker,
    _convert_to_aac,
    _fetch_and_convert_audio,
    async_setup_entry,
)


class TestAsyncSetupEntry:
    @pytest.mark.asyncio
    async def test_speaker_added_for_siren_model(
        self, hass, mock_coordinator, mock_config_entry
    ):
        """Speaker entity added for cameras with sirens."""
        mock_coordinator.model = "IPC-HDW3849HP-AS-PV"
        mock_config_entry.runtime_data = mock_coordinator
        added = []

        await async_setup_entry(hass, mock_config_entry, added.append)

        assert len(added) == 1
        assert isinstance(added[0][0], DahuaSpeaker)

    @pytest.mark.asyncio
    async def test_speaker_added_for_doorbell(
        self, hass, mock_coordinator, mock_config_entry
    ):
        """Speaker entity added for doorbells."""
        mock_coordinator.model = "VTO2111D-WP"
        mock_config_entry.runtime_data = mock_coordinator
        added = []

        await async_setup_entry(hass, mock_config_entry, added.append)

        assert len(added) == 1
        assert isinstance(added[0][0], DahuaSpeaker)

    @pytest.mark.asyncio
    async def test_speaker_added_for_amcrest_doorbell(
        self, hass, mock_coordinator, mock_config_entry
    ):
        """Speaker entity added for Amcrest doorbells."""
        mock_coordinator.model = "AD410"
        mock_config_entry.runtime_data = mock_coordinator
        added = []

        await async_setup_entry(hass, mock_config_entry, added.append)

        assert len(added) == 1

    @pytest.mark.asyncio
    async def test_speaker_not_added_for_basic_camera(
        self, hass, mock_coordinator, mock_config_entry
    ):
        """Speaker entity NOT added for basic cameras without siren/doorbell."""
        mock_coordinator.model = "IPC-HDW5831R-ZE"
        mock_config_entry.runtime_data = mock_coordinator
        added = []

        await async_setup_entry(hass, mock_config_entry, added.append)

        assert len(added) == 0


class TestDahuaSpeaker:
    def test_unique_id(self, mock_coordinator, mock_config_entry):
        speaker = DahuaSpeaker(mock_coordinator, mock_config_entry)
        assert speaker.unique_id == "SERIAL123_speaker"

    def test_translation_key(self, mock_coordinator, mock_config_entry):
        speaker = DahuaSpeaker(mock_coordinator, mock_config_entry)
        assert speaker._attr_translation_key == "speaker"

    def test_initial_state(self, mock_coordinator, mock_config_entry):
        speaker = DahuaSpeaker(mock_coordinator, mock_config_entry)
        assert speaker.state == MediaPlayerState.IDLE

    def test_supported_features(self, mock_coordinator, mock_config_entry):
        speaker = DahuaSpeaker(mock_coordinator, mock_config_entry)
        assert speaker.supported_features == MediaPlayerEntityFeature.PLAY_MEDIA

    @pytest.mark.asyncio
    async def test_play_media(self, hass, mock_coordinator, mock_config_entry):
        """async_play_media fetches, converts, and posts audio."""
        mock_coordinator.client.async_post_audio = AsyncMock()
        speaker = DahuaSpeaker(mock_coordinator, mock_config_entry)
        speaker.hass = hass
        speaker.async_write_ha_state = MagicMock()

        fake_aac = b"\x00\x01\x02"
        with patch(
            "custom_components.dahua.media_player._fetch_and_convert_audio",
            return_value=(fake_aac, 5.0),
        ) as mock_fetch:
            await speaker.async_play_media("music", "http://example.com/audio.wav")

            mock_fetch.assert_called_once_with(hass, "http://example.com/audio.wav")
            mock_coordinator.client.async_post_audio.assert_called_once_with(
                fake_aac, 1, encoding="AAC", duration=5.0
            )

        # State should be back to IDLE
        assert speaker._attr_state == MediaPlayerState.IDLE

    @pytest.mark.asyncio
    async def test_play_media_state_transitions(
        self, hass, mock_coordinator, mock_config_entry
    ):
        """State transitions: IDLE -> PLAYING -> IDLE."""
        mock_coordinator.client.async_post_audio = AsyncMock()
        speaker = DahuaSpeaker(mock_coordinator, mock_config_entry)
        speaker.hass = hass

        states = []
        speaker.async_write_ha_state = lambda: states.append(speaker._attr_state)

        with patch(
            "custom_components.dahua.media_player._fetch_and_convert_audio",
            return_value=(b"\x00", 1.0),
        ):
            await speaker.async_play_media("music", "http://example.com/audio.wav")

        assert states == [MediaPlayerState.PLAYING, MediaPlayerState.IDLE]

    @pytest.mark.asyncio
    async def test_play_media_error_resets_state(
        self, hass, mock_coordinator, mock_config_entry
    ):
        """On error, state resets to IDLE."""
        speaker = DahuaSpeaker(mock_coordinator, mock_config_entry)
        speaker.hass = hass
        speaker.async_write_ha_state = MagicMock()

        with (
            patch(
                "custom_components.dahua.media_player._fetch_and_convert_audio",
                side_effect=aiohttp.ClientError("fetch failed"),
            ),
            pytest.raises(Exception),
        ):
            await speaker.async_play_media("music", "http://example.com/audio.wav")

        assert speaker._attr_state == MediaPlayerState.IDLE


class TestFetchAndConvertAudio:
    @pytest.mark.asyncio
    async def test_fetches_url_and_converts(self, hass):
        """Fetches audio from URL via aiohttp and runs ffmpeg conversion."""
        mock_response = AsyncMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.read = AsyncMock(return_value=b"\x00\x01\x02")
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_response)

        converted = (b"\xd5\xd5", 5.0)
        with (
            patch(
                "custom_components.dahua.media_player.async_get_clientsession",
                return_value=mock_session,
            ),
            patch(
                "custom_components.dahua.media_player._convert_to_aac",
                return_value=converted,
            ),
        ):
            result = await _fetch_and_convert_audio(hass, "http://example.com/tts.wav")

        assert result == converted
        mock_session.get.assert_called_once_with("http://example.com/tts.wav")


class TestConvertToAac:
    def test_ffmpeg_called_with_correct_args(self):
        """ffmpeg is invoked with correct arguments."""
        fake_output = b"\xd5\xd5\xd5"
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = fake_output
        mock_result.stderr = b"Duration: 00:00:05.12, start"

        with patch(
            "custom_components.dahua.media_player.subprocess.run",
            return_value=mock_result,
        ) as mock_run:
            data, duration = _convert_to_aac(b"\x00\x01\x02")

            mock_run.assert_called_once_with(
                [
                    "ffmpeg",
                    "-i",
                    "pipe:0",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "64k",
                    "-ar",
                    "8000",
                    "-ac",
                    "1",
                    "-f",
                    "adts",
                    "pipe:1",
                ],
                input=b"\x00\x01\x02",
                capture_output=True,
            )
            assert data == fake_output
            assert duration == 5.12

    def test_ffmpeg_failure_raises_runtime_error(self):
        """Failed ffmpeg raises RuntimeError."""
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = b"some error"

        with (
            patch(
                "custom_components.dahua.media_player.subprocess.run",
                return_value=mock_result,
            ),
            pytest.raises(RuntimeError, match="ffmpeg failed"),
        ):
            _convert_to_aac(b"\x00\x01\x02")

    def test_zero_duration_when_not_found(self):
        """Duration defaults to 0 when not in ffmpeg output."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = b"\xd5"
        mock_result.stderr = b"no duration here"

        with patch(
            "custom_components.dahua.media_player.subprocess.run",
            return_value=mock_result,
        ):
            _, duration = _convert_to_aac(b"\x00")
            assert duration == 0.0


class TestParseAdtsFrames:
    def _make_frame(self, payload_size: int) -> bytes:
        """Build a minimal ADTS frame with given payload size."""
        # ADTS header is 7 bytes, total frame length = 7 + payload_size
        frame_length = 7 + payload_size
        header = bytearray(7)
        header[0] = 0xFF
        header[1] = 0xF1  # sync + MPEG-4, Layer 0, no CRC
        header[2] = 0x50  # AAC-LC, 44100 Hz (index 4), private=0
        # byte 3: channel config (bits 7-6) + frame_length high bits (1-0)
        header[3] = 0x80 | ((frame_length >> 11) & 0x03)
        header[4] = (frame_length >> 3) & 0xFF
        header[5] = ((frame_length & 0x07) << 5) | 0x1F
        header[6] = 0xFC  # buffer fullness + 0 raw data blocks
        return bytes(header) + b"\x00" * payload_size

    def test_parses_multiple_frames(self):
        """Multiple ADTS frames are parsed correctly."""
        f1 = self._make_frame(100)
        f2 = self._make_frame(200)
        data = f1 + f2
        frames = _parse_adts_frames(data)
        assert len(frames) == 2
        assert frames[0] == f1
        assert frames[1] == f2

    def test_empty_data(self):
        """Empty input returns no frames."""
        assert _parse_adts_frames(b"") == []

    def test_skips_non_sync_bytes(self):
        """Garbage bytes before a valid frame are skipped."""
        garbage = b"\x00\x01\x02"
        frame = self._make_frame(50)
        frames = _parse_adts_frames(garbage + frame)
        assert len(frames) == 1
        assert frames[0] == frame

    def test_truncated_frame_ignored(self):
        """A frame whose declared length exceeds available data is ignored."""
        frame = self._make_frame(100)
        truncated = frame[:50]  # cut short
        assert _parse_adts_frames(truncated) == []
