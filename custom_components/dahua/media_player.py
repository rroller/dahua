"""Media player platform for Dahua cameras with speakers."""

from __future__ import annotations

import logging
import subprocess
from typing import Any

from homeassistant.components.media_player import (
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
    MediaType,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from custom_components.dahua import DahuaConfigEntry, DahuaDataUpdateCoordinator

from .entity import DahuaBaseEntity, dahua_command

_LOGGER: logging.Logger = logging.getLogger(__package__)

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DahuaConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Dahua media player platform."""
    coordinator: DahuaDataUpdateCoordinator = entry.runtime_data

    if coordinator.supports_speaker():
        async_add_entities([DahuaSpeaker(coordinator, entry)])


def _convert_to_aac(audio_data: bytes) -> tuple[bytes, float]:
    """Convert audio bytes to AAC (8 kHz, mono, ADTS) using ffmpeg.

    Returns a tuple of (aac_bytes, duration_seconds).
    """
    result = subprocess.run(
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
        input=audio_data,
        capture_output=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "ffmpeg failed with code {0}: {1}".format(
                result.returncode, result.stderr.decode(errors="replace")
            )
        )
    # Parse duration from ffmpeg stderr (e.g. "Duration: 00:00:05.12")
    duration = 0.0
    for line in result.stderr.decode(errors="replace").splitlines():
        if "Duration:" in line:
            import re

            match = re.search(r"Duration:\s*(\d+):(\d+):(\d+)\.(\d+)", line)
            if match:
                h, m, s, cs = (int(g) for g in match.groups())
                duration = h * 3600 + m * 60 + s + cs / 100.0
                break
    return result.stdout, duration


async def _fetch_and_convert_audio(
    hass: HomeAssistant, url: str
) -> tuple[bytes, float]:
    """Fetch audio from URL and convert to AAC format.

    Returns a tuple of (aac_bytes, duration_seconds).
    """
    session = async_get_clientsession(hass)
    async with session.get(url) as response:
        response.raise_for_status()
        audio_data = await response.read()

    return await hass.async_add_executor_job(_convert_to_aac, audio_data)


class DahuaSpeaker(DahuaBaseEntity, MediaPlayerEntity):
    """Dahua camera speaker media player entity."""

    _attr_translation_key = "speaker"
    _attr_supported_features = MediaPlayerEntityFeature.PLAY_MEDIA
    _attr_state = MediaPlayerState.IDLE

    @property
    def unique_id(self) -> str:
        """Return a unique ID for this entity."""
        return self._coordinator.get_serial_number() + "_speaker"

    @dahua_command
    async def async_play_media(
        self,
        media_type: MediaType | str,
        media_id: str,
        **kwargs: Any,
    ) -> None:
        """Play audio on the camera speaker."""
        self._attr_state = MediaPlayerState.PLAYING
        self.async_write_ha_state()
        try:
            aac_data, duration = await _fetch_and_convert_audio(self.hass, media_id)
            channel = self._coordinator.get_channel_number()
            await self._coordinator.client.async_post_audio(
                aac_data, channel, encoding="AAC", duration=duration
            )
        finally:
            self._attr_state = MediaPlayerState.IDLE
            self.async_write_ha_state()
