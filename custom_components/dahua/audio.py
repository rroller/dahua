"""Turning "play this" into bytes a Dahua speaker will take (#177).

The client can push a clip to `audio.cgi?action=postAudio` but takes it
already in the device's own format, as raw bytes with a byte rate to pace
them by. Nothing a user has is in that shape: a TTS announcement is an MP3
behind a URL, a doorbell chime is a file on disk. This module is the gap
between the two, and it is deliberately not in `client.py`, which talks to
devices and should not know what a media source is.

**The format is chosen, not detected.** A VTO2000A's speaker has been
verified by listening to chimes: G.711 A-law at 8 kHz plays at the correct
pitch and tempo, and PCM at 8 kHz requires little-endian samples. G.711
A-law is the default. Formats above 8 kHz are not offered because a
32,000-byte-per-second push was present in device hangs during testing.

Every clip is converted with Home Assistant's configured ffmpeg binary.
The ffmpeg integration is imported when this service runs, so its absence
produces a service error rather than preventing Dahua from setting up.

"""

import asyncio
import logging
import os
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
        # LITTLE-endian, measured by ear on a VTO2000A: the same chime sent
        # big-endian came back "fast and kind of like white noise", and sent
        # little-endian came back as "a tune". This row used to say `s16be`,
        # reasoned from the device's own microphone being `pcm_s16be 16000`.
        # That inference is wrong in both halves -- the receive side is 16 kHz
        # big-endian and the send side is 8 kHz little-endian -- and it is one
        # more way the two halves of this device's audio do not match.
        content_type="Audio/PCM",
        sample_rate=8000,
        bytes_per_sample=2,
        ffmpeg_format="s16le",
        description="16-bit little-endian PCM, 8 kHz",
    ),
    # There was a `pcm_16000` row here and it is deliberately gone. Two
    # measurements killed it. It played as noise, because the speaker runs at
    # 8 kHz whatever the microphone does, so a clip generated for 16 kHz is
    # twice as fast. And at two bytes a sample it declares a 32,000 B/s push,
    # which is double what the device drains: that is the one rate present in
    # two of the three times a VTO2000A has been left hung by postAudio here,
    # and the hang takes HTTP, RTSP and DHIP with it. Do not reintroduce it
    # with the byte order corrected; the rate is the dangerous half.
}

DEFAULT_AUDIO_FORMAT = "g711a_8000"

# The schemes a clip may name. Everything else is read as a path and checked
# against Home Assistant's allowlist, because ffmpeg's own pseudo-protocols
# (`concat:`, `subfile:` and the rest) look like schemes and read local files.
SAFE_SCHEMES = ("http://", "https://", "rtsp://", "rtsps://")

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

    Four shapes arrive here. A `media-source://` id, which is what the media
    selector in the service dialog produces and what a TTS announcement comes
    as; a Home Assistant URL relative to this instance, which is what
    `tts.speak` leaves behind; a plain http or rtsp URL; and a file on disk,
    which is what somebody with a chime in `www/` writes.

    **Anything that is not one of those is treated as a path and has to be in
    the allowlist.** ffmpeg takes more than URLs: `concat:`, `subfile:` and
    friends are pseudo-protocols that read local files, so "it has a scheme,
    therefore it is a URL" would hand a service call the run of the disk.
    Naming the schemes that are allowed, rather than the ones that are not,
    is what keeps that closed. It is the same allowlist the backup services
    and camera snapshots use.
    """
    from homeassistant.components import media_source
    from homeassistant.components.media_player.browse_media import (
        async_process_play_media_url,
    )

    if media_source.is_media_source_id(media_content_id):
        # Home Assistant resolved this one, so it is as trustworthy as the
        # media source that produced it.
        resolved = await media_source.async_resolve_media(hass, media_content_id, None)
        return _absolute(hass, resolved.url, async_process_play_media_url)

    url = media_content_id
    if url.startswith(SAFE_SCHEMES):
        return url
    if url.startswith("/"):
        # Ambiguous: `/api/tts_proxy/x.mp3` is a URL on this instance and
        # `/media/chime.mp3` is a file. Whether it exists decides, because a
        # URL path that happens to name a real file would be served from that
        # file anyway.
        if not await hass.async_add_executor_job(os.path.isfile, url):
            return _absolute(hass, url, async_process_play_media_url)
    if not hass.config.is_allowed_path(url):
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="audio_path_not_allowed",
            translation_placeholders={"path": url},
        )
    return url


def _absolute(hass: HomeAssistant, url: str, process) -> str:
    """A URL relative to this instance, made absolute for ffmpeg.

    ffmpeg is a separate process with no idea which host Home Assistant is,
    and `async_process_play_media_url` is what works that out.
    """
    return process(hass, url) if url.startswith("/") else url


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
    except (TimeoutError, asyncio.CancelledError) as error:
        try:
            process.kill()
        except ProcessLookupError:
            pass  # The child exited just before the cancellation arrived.
        # Reaped rather than left behind: without this the child stays as a
        # zombie and its pipes stay open for the life of Home Assistant.
        await process.wait()
        if isinstance(error, asyncio.CancelledError):
            raise
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
