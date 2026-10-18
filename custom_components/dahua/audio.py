"""Turning "play this" into bytes a Dahua speaker will take (#177).

The client can push a clip to `audio.cgi?action=postAudio` but takes it
already in the device's own format, as raw bytes with a byte rate to pace
them by. Nothing a user has is in that shape: a TTS announcement is an MP3
behind a URL, a doorbell chime is a file on disk. This module is the gap
between the two, and it is deliberately not in `client.py`, which talks to
devices and should not know what a media source is.

**The format is chosen, not detected.** Which one a device wants has not been
measured. Every published recipe for this CGI converts to A-law at 8 kHz, and
the one push measured against a VTO2000A settled at about 21,000 bytes a
second, which is nearer A-law's 8,000 than the 32,000 of the 16 kHz PCM its
microphone produces. So A-law is the default, as the better supported guess
rather than as a finding, and the rest are offered because this integration
covers doorbells, cameras, recorders and four rebrands, and one of them will
want something else.

The default also errs in the safe direction. Declaring a rate higher than the
device plays at makes the push outlast its welcome, and a push abandoned part
way through is the thing that hung a VTO2000A for ten minutes; declaring one
too low only makes the audio arrive faster than it plays. A-law at 8 kHz is
the lowest rate on the table.

**ffmpeg is not required to be present.** It is only reached for when the clip
needs converting, and a clip already in the device's format is sent straight
through. Home Assistant ships the `ffmpeg` integration and nearly every
install has it, but "nearly" is why this asks rather than assumes, and says so
plainly when it is missing.
"""

import asyncio
import logging
from dataclasses import dataclass

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class AudioFormat:
    """One thing a device might accept, and how to make it.

    `byte_rate` is how many bytes make one second of sound, which is what
    paces the push. It is derived from the other three rather than stored, so
    a format cannot be added with a rate that contradicts its own encoding --
    that arithmetic being wrong is exactly how a clip plays at the wrong
    speed.
    """

    content_type: str
    sample_rate: int
    bytes_per_sample: int
    ffmpeg_format: str
    description: str

    @property
    def byte_rate(self) -> int:
        """Mono throughout. No Dahua speaker measured here takes two channels."""
        return self.sample_rate * self.bytes_per_sample


# The keys are what a user types into the service call, so they say the codec
# and the rate rather than being numbered.
AUDIO_FORMATS: dict[str, AudioFormat] = {
    "g711a_8000": AudioFormat(
        # The content type every published recipe sends. Dahua's own API doc
        # spells the G.711 types this way in its audio section, and the device
        # measured here did not object to any of the types tried.
        content_type="Audio/G.711A",
        sample_rate=8000,
        bytes_per_sample=1,
        ffmpeg_format="alaw",
        description="G.711 A-law, 8 kHz. What every published recipe uses",
    ),
    "g711u_8000": AudioFormat(
        content_type="Audio/G.711U",
        sample_rate=8000,
        bytes_per_sample=1,
        ffmpeg_format="mulaw",
        description="G.711 mu-law, 8 kHz",
    ),
    "pcm_8000": AudioFormat(
        content_type="Audio/PCM",
        sample_rate=8000,
        bytes_per_sample=2,
        ffmpeg_format="s16be",
        description="16-bit PCM, 8 kHz",
    ),
    "pcm_16000": AudioFormat(
        # Big-endian, which is not a guess: the VTO2000A's own RTSP audio
        # track is `pcm_s16be 16000`, and its `getAudio` stream answers
        # `Audio/PCM` at 32,000 bytes a second. Sending the other order is a
        # quieter bug than it sounds -- a byte-swapped sine is still periodic
        # at the same pitch, so it does not arrive as obvious noise, it
        # arrives as something loud and wrong.
        content_type="Audio/PCM",
        sample_rate=16000,
        bytes_per_sample=2,
        ffmpeg_format="s16be",
        description="16-bit big-endian PCM, 16 kHz. What a VTO2000A's own microphone produces",
    ),
}

DEFAULT_AUDIO_FORMAT = "g711a_8000"

# Nothing longer than this is converted. A service call pointed at a live
# stream rather than a clip would otherwise read until the host ran out of
# memory, and the whole clip is held in memory because the push needs its
# length declared up front.
MAX_CLIP_SECONDS = 300

# How long ffmpeg is given beyond the clip itself. Converting is far faster
# than real time, so this is the allowance for starting up and for reading a
# URL over the network, not for the work.
TRANSCODE_TIMEOUT_SECONDS = 60


def resolve_format(name: str | None) -> AudioFormat:
    """Name to format, with the error naming what it could have been."""
    chosen = name or DEFAULT_AUDIO_FORMAT
    audio_format = AUDIO_FORMATS.get(chosen)
    if audio_format is None:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="audio_format_unknown",
            translation_placeholders={
                "format": chosen,
                "known": ", ".join(sorted(AUDIO_FORMATS)),
            },
        )
    return audio_format


async def async_resolve_media(hass: HomeAssistant, media_content_id: str) -> str:
    """A media content id to something ffmpeg can open.

    Three shapes arrive here. A `media-source://` id, which is what the media
    selector in the service dialog produces and what a TTS announcement comes
    as; a Home Assistant URL that is relative to this instance, which is what
    `tts.speak` leaves behind; and a plain http URL or file path, which is
    what somebody typing into YAML writes.
    """
    from homeassistant.components import media_source
    from homeassistant.components.media_player.browse_media import (
        async_process_play_media_url,
    )

    url = media_content_id
    if media_source.is_media_source_id(media_content_id):
        resolved = await media_source.async_resolve_media(hass, media_content_id, None)
        url = resolved.url
    if url.startswith("/"):
        # Relative to this instance. Making it absolute needs an external URL
        # to be known, which `async_process_play_media_url` works out.
        url = async_process_play_media_url(hass, url)
    return url


async def async_transcode(
    hass: HomeAssistant, url: str, audio_format: AudioFormat
) -> bytes:
    """Whatever is at `url`, as raw bytes in the device's format.

    Runs Home Assistant's own ffmpeg binary rather than looking for one on
    PATH, so an install that has configured a different path is honoured.

    Decoding is left entirely to ffmpeg: no format is named on the input side,
    so an MP3, a WAV, a FLAC or an RTSP stream all work and this module needs
    to know nothing about any of them.
    """
    binary = _ffmpeg_binary(hass)
    arguments = [
        binary,
        # A subprocess that inherits stdin can block for ever waiting on it.
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        url,
        # Drop any video. A clip pulled from a camera recording has a picture
        # on it, and without this ffmpeg refuses the raw audio output rather
        # than ignoring the stream.
        "-vn",
        "-t",
        str(MAX_CLIP_SECONDS),
        "-ar",
        str(audio_format.sample_rate),
        "-ac",
        "1",
        "-f",
        audio_format.ffmpeg_format,
        "pipe:1",
    ]
    _LOGGER.debug("Converting %s to %s", url, audio_format.ffmpeg_format)

    process = await asyncio.create_subprocess_exec(
        *arguments,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        async with asyncio.timeout(TRANSCODE_TIMEOUT_SECONDS):
            audio, errors = await process.communicate()
    except TimeoutError:
        process.kill()
        # Reaped rather than left behind: without this the child stays as a
        # zombie and its pipes stay open for the life of Home Assistant.
        await process.wait()
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="audio_conversion_timed_out",
            translation_placeholders={
                "url": url,
                "seconds": str(TRANSCODE_TIMEOUT_SECONDS),
            },
        ) from None

    if process.returncode != 0:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="audio_conversion_failed",
            translation_placeholders={
                "url": url,
                "reason": errors.decode(errors="replace").strip()[:200]
                or "ffmpeg failed",
            },
        )
    if not audio:
        # ffmpeg is happy to succeed with nothing to show for it, which is
        # what a URL carrying no audio track looks like. Silence pushed to a
        # speaker is indistinguishable from the service doing nothing, so it
        # is worth saying out loud.
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="audio_has_no_sound",
            translation_placeholders={"url": url},
        )
    return audio


def _ffmpeg_binary(hass: HomeAssistant) -> str:
    """Home Assistant's configured ffmpeg, or a message that says what to do.

    Imported here rather than at the top of the module so that the whole
    integration does not depend on the ffmpeg one. This is the only feature
    that needs it, and an install without it should lose this feature rather
    than fail to start.
    """
    try:
        from homeassistant.components.ffmpeg import get_ffmpeg_manager

        return get_ffmpeg_manager(hass).binary
    except (ImportError, KeyError, ValueError) as error:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="audio_needs_ffmpeg",
        ) from error
