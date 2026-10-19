"""Getting a clip from "what the user asked for" to "what the device takes" (#177).

The client pushes raw bytes at a declared rate. Nothing a user has is in that
shape -- a TTS announcement is an MP3 behind a URL -- so something has to
resolve what was pointed at and convert it, and this is what that has to get
right.

Two of these are load bearing rather than tidy:

* **The byte rate has to match the encoding.** It is what paces the push, so
  arithmetic that disagrees with the sample rate plays the clip at the wrong
  speed, and there is no feedback from the device to notice it by. It is
  derived from the encoding here rather than written down twice.
* **The default has to be the slow end.** Declaring a rate higher than the
  device plays at makes the push outlast its own clip, which is the shape a
  VTO2000A was found hung after. Declaring one too low just finishes early.
"""

import asyncio
import shutil

import pytest
import yaml

from homeassistant.exceptions import HomeAssistantError
from homeassistant.setup import async_setup_component

from custom_components.dahua import audio as audio_module
from custom_components.dahua import camera as camera_module
from custom_components.dahua.audio import (
    AUDIO_FORMATS,
    DEFAULT_AUDIO_FORMAT,
    MAX_CLIP_SECONDS,
    async_resolve_media,
    async_transcode,
    resolve_format,
)

SERVICES = yaml.safe_load(
    open("custom_components/dahua/services.yaml", encoding="utf-8")
)


# --- the format table -------------------------------------------------------


# What one sample costs in each encoding ffmpeg is asked for. Written out
# here rather than imported, because this is the fact the table is being
# checked against and importing it would check the table against itself.
SAMPLE_WIDTHS = {"alaw": 1, "mulaw": 1, "s16be": 2, "s16le": 2, "u8": 1}


@pytest.mark.parametrize("name", sorted(AUDIO_FORMATS))
def test_every_formats_width_matches_the_encoding_it_asks_ffmpeg_for(name):
    """The coupling the byte rate rests on, and the one that can be wrong.

    `byte_rate` is arithmetic on `sample_rate` and `bytes_per_sample`, so it
    cannot disagree with them -- but either can disagree with the encoding
    actually requested, and nothing downstream would notice. A format
    declaring two bytes a sample while asking ffmpeg for A-law paces the push
    at twice the rate the device plays it, which is the direction that ends
    in a push abandoned part way through.
    """
    audio_format = AUDIO_FORMATS[name]
    assert audio_format.ffmpeg_format in SAMPLE_WIDTHS, (
        "unknown encoding %s, so its sample width cannot be checked"
        % audio_format.ffmpeg_format
    )
    assert audio_format.bytes_per_sample == SAMPLE_WIDTHS[audio_format.ffmpeg_format]
    assert audio_format.byte_rate == audio_format.sample_rate * (
        SAMPLE_WIDTHS[audio_format.ffmpeg_format]
    )


@pytest.mark.parametrize("name", sorted(AUDIO_FORMATS))
def test_every_format_names_its_rate_the_way_the_key_does(name):
    """The key is what a user types, so it has to be true."""
    assert name.endswith(str(AUDIO_FORMATS[name].sample_rate))


def test_the_default_is_the_lowest_rate_on_the_table():
    """Being wrong downwards is cheap and being wrong upwards hangs a device.

    A declared rate above what the speaker really plays at makes the push
    take longer than its own clip, which is the shape that ends in one
    abandoned part way through. So whichever format is the default, it should
    be the one that cannot do that to any of the others.
    """
    slowest = min(fmt.byte_rate for fmt in AUDIO_FORMATS.values())
    assert AUDIO_FORMATS[DEFAULT_AUDIO_FORMAT].byte_rate == slowest


def test_no_format_asks_the_device_to_swallow_more_than_it_drains():
    """The format table cannot offer a rate that has been seen hanging a device.

    Measured on a VTO2000A: the speaker consumes 8,000 samples a second, so
    16,000 B/s is the ceiling for a two-byte format and 8,000 for a one-byte
    one. A `pcm_16000` row used to sit here declaring 32,000 B/s, and that is
    the one rate present in two of the three times postAudio has left that
    device hung with HTTP, RTSP and DHIP all dead.

    This is a guard against reintroducing it, including the tempting version
    with the byte order corrected: the endianness was a separate bug and the
    rate is the dangerous half.
    """
    for name, audio_format in AUDIO_FORMATS.items():
        assert audio_format.sample_rate <= 8000, (
            "%s plays faster than the speaker runs; a clip generated for a "
            "higher rate comes out fast and unintelligible" % name
        )
        assert (
            audio_format.byte_rate <= 16000
        ), "%s declares %d B/s, above what the device drains" % (
            name,
            audio_format.byte_rate,
        )


def test_pcm_is_little_endian_whatever_the_microphone_does():
    """Measured by ear, and the opposite of what the receive side suggests.

    The device's own microphone serves `pcm_s16be` at 16 kHz, and reasoning
    from that to the speaker is what put `s16be` here in the first place. The
    same chime sent big-endian came back "fast and kind of like white noise";
    sent little-endian it came back as "a tune".
    """
    for name, audio_format in AUDIO_FORMATS.items():
        if audio_format.bytes_per_sample == 2:
            assert audio_format.ffmpeg_format == "s16le", (
                "%s is big-endian; that plays as loud noise on a VTO2000A" % name
            )


def test_an_unknown_format_says_what_the_known_ones_are():
    with pytest.raises(HomeAssistantError) as raised:
        resolve_format("mp3")

    # Asserted on the key and its placeholders rather than on the rendered
    # string: a translated error only renders inside a running Home
    # Assistant, and the key is the half that decides what every language
    # shows.
    assert raised.value.translation_key == "audio_format_unknown"
    placeholders = raised.value.translation_placeholders
    assert placeholders["format"] == "mp3"
    for name in AUDIO_FORMATS:
        assert name in placeholders["known"]


def test_no_format_means_the_default_rather_than_an_error():
    assert resolve_format(None) is AUDIO_FORMATS[DEFAULT_AUDIO_FORMAT]


def test_the_service_offers_exactly_the_formats_that_exist():
    """Derived from the table rather than copied, so the two cannot diverge.

    A format added to the code and not to the dialog is invisible; one left
    in the dialog after being removed from the code is a service call that
    fails validation after the user has already picked it.
    """
    options = SERVICES["play_audio"]["fields"]["audio_format"]["selector"]["select"][
        "options"
    ]
    assert sorted(option["value"] for option in options) == sorted(AUDIO_FORMATS)
    assert SERVICES["play_audio"]["fields"]["audio_format"]["default"] == (
        DEFAULT_AUDIO_FORMAT
    )


def test_the_service_asks_for_the_clip_and_requires_it():
    field = SERVICES["play_audio"]["fields"]["media_content_id"]
    assert field["required"] is True


# --- converting -------------------------------------------------------------


class _FakeProcess:
    def __init__(self, stdout=b"", stderr=b"", returncode=0, hang=False):
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode
        self._hang = hang
        self.killed = False
        self.waited = False

    async def communicate(self):
        if self._hang:
            await asyncio.sleep(3600)
        return self._stdout, self._stderr

    def kill(self):
        self.killed = True

    async def wait(self):
        self.waited = True


def _record_subprocess(monkeypatch, process):
    """Stands in for ffmpeg, and keeps the argv it was called with."""
    captured = {}

    async def fake_exec(*arguments, **kwargs):
        captured["argv"] = list(arguments)
        captured["kwargs"] = kwargs
        return process

    monkeypatch.setattr(audio_module.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(audio_module, "_ffmpeg_binary", lambda hass: "/usr/bin/ffmpeg")
    return captured


async def test_the_clip_comes_back_as_the_bytes_ffmpeg_wrote(monkeypatch):
    captured = _record_subprocess(monkeypatch, _FakeProcess(stdout=b"\xd5" * 800))

    audio = await async_transcode(None, "http://host/clip.mp3", resolve_format(None))

    assert audio == b"\xd5" * 800
    assert captured["argv"][0] == "/usr/bin/ffmpeg"


async def test_the_conversion_asks_for_the_format_that_was_chosen(monkeypatch):
    """Mono, the right rate, the right encoding, and nothing else.

    Asserted as adjacent pairs rather than by matching the whole command,
    which would fail for a harmless reordering and teach nothing.
    """
    captured = _record_subprocess(monkeypatch, _FakeProcess(stdout=b"x"))

    await async_transcode(None, "http://host/clip.mp3", resolve_format("pcm_8000"))

    argv = captured["argv"]
    pairs = list(zip(argv, argv[1:]))
    assert ("-ar", "8000") in pairs, "the sample rate is not being asked for"
    assert ("-ac", "1") in pairs, "a stereo clip would be sent to a mono speaker"
    assert ("-f", "s16le") in pairs, "big-endian plays as noise, measured by ear"
    assert ("-i", "http://host/clip.mp3") in pairs
    assert argv[-1] == "pipe:1", "the audio has to come back on stdout"


async def test_a_clip_with_a_picture_on_it_still_converts(monkeypatch):
    """A recording pulled off a camera has video on it, and ffmpeg refuses
    raw audio output unless the video is dropped."""
    captured = _record_subprocess(monkeypatch, _FakeProcess(stdout=b"x"))

    await async_transcode(None, "http://host/clip.dav", resolve_format(None))

    assert "-vn" in captured["argv"]


async def test_the_length_is_capped_so_a_stream_cannot_fill_memory(monkeypatch):
    """The whole clip is held in memory, because the push declares its length.

    Pointed at an RTSP stream rather than a file, an uncapped conversion
    returns when the stream does, which is never.
    """
    captured = _record_subprocess(monkeypatch, _FakeProcess(stdout=b"x"))

    await async_transcode(None, "rtsp://host/live", resolve_format(None))

    argv = captured["argv"]
    assert ("-t", str(MAX_CLIP_SECONDS)) in list(zip(argv, argv[1:]))


async def test_a_conversion_that_fails_says_what_ffmpeg_said(monkeypatch):
    _record_subprocess(
        monkeypatch,
        _FakeProcess(stderr=b"No such file or directory", returncode=1),
    )

    with pytest.raises(HomeAssistantError) as raised:
        await async_transcode(None, "http://host/gone.mp3", resolve_format(None))

    assert raised.value.translation_key == "audio_conversion_failed"
    assert "No such file or directory" in (
        raised.value.translation_placeholders["reason"]
    )


async def test_a_url_with_no_audio_in_it_is_not_silently_played(monkeypatch):
    """ffmpeg succeeds with nothing to show for it, and silence pushed to a
    speaker looks exactly like the service having done nothing at all."""
    _record_subprocess(monkeypatch, _FakeProcess(stdout=b"", returncode=0))

    with pytest.raises(HomeAssistantError) as raised:
        await async_transcode(None, "http://host/silent.mp4", resolve_format(None))

    assert raised.value.translation_key == "audio_has_no_sound"
    assert raised.value.translation_placeholders["url"] == "http://host/silent.mp4"


async def test_a_conversion_that_hangs_is_killed_and_reaped(monkeypatch):
    """Killed is half of it. Left unwaited, the child stays a zombie and its
    pipes stay open for as long as Home Assistant runs."""
    process = _FakeProcess(hang=True)
    _record_subprocess(monkeypatch, process)
    monkeypatch.setattr(audio_module, "TRANSCODE_TIMEOUT_SECONDS", 0.05)

    with pytest.raises(HomeAssistantError) as raised:
        await async_transcode(None, "http://host/slow.mp3", resolve_format(None))

    assert raised.value.translation_key == "audio_conversion_timed_out"
    assert process.killed
    assert process.waited, "a killed child still has to be waited on"


async def test_cancelling_a_conversion_kills_and_reaps_ffmpeg(monkeypatch):
    process = _FakeProcess(hang=True)
    _record_subprocess(monkeypatch, process)
    task = asyncio.create_task(
        async_transcode(None, "http://host/slow.mp3", resolve_format(None))
    )
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.killed, "service cancellation must not leave ffmpeg running"
    assert process.waited, "a cancelled conversion must reap its child"


def test_a_missing_ffmpeg_integration_says_so_rather_than_raising_valueerror():
    """`get_ffmpeg_manager` raises ValueError on an install without it, which
    reaches the user as "unknown error" in the service dialog."""

    class _Hass:
        data = {}

    with pytest.raises(HomeAssistantError) as raised:
        audio_module._ffmpeg_binary(_Hass())

    assert raised.value.translation_key == "audio_needs_ffmpeg"


# --- the service ------------------------------------------------------------


class _Client:
    def __init__(self, raises=None):
        self.calls = []
        self._raises = raises

    async def async_play_audio(self, audio, content_type, byte_rate, channel_number):
        self.calls.append((audio, content_type, byte_rate, channel_number))
        if self._raises:
            raise self._raises


class _Coordinator:
    def __init__(self, client):
        self.client = client

    def get_device_name(self):
        return "Front Door"


def _camera(client, channel_number=1):
    entity = object.__new__(camera_module.DahuaCamera)
    entity._coordinator = _Coordinator(client)
    entity._channel_number = channel_number
    entity.hass = None
    return entity


def _patch_the_front_half(monkeypatch, audio=b"\xd5" * 100):
    """Resolving and converting are covered above, so they stand aside here."""

    async def fake_resolve(hass, media_content_id):
        return "resolved:" + media_content_id

    async def fake_transcode(hass, url, audio_format):
        fake_transcode.seen = (url, audio_format)
        return audio

    monkeypatch.setattr(camera_module, "async_resolve_media", fake_resolve)
    monkeypatch.setattr(camera_module, "async_transcode", fake_transcode)
    return fake_transcode


async def test_the_device_is_told_the_format_the_clip_was_made_in(monkeypatch):
    """The three have to agree or the clip plays at the wrong speed: what was
    converted, what the device is told it is, and what it is paced at."""
    _patch_the_front_half(monkeypatch)
    client = _Client()

    await camera_module.DahuaCamera.async_play_audio(
        _camera(client), "media-source://tts/x", "pcm_8000"
    )

    _, content_type, byte_rate, _ = client.calls[0]
    assert content_type == AUDIO_FORMATS["pcm_8000"].content_type
    assert byte_rate == AUDIO_FORMATS["pcm_8000"].byte_rate


async def test_the_clip_goes_to_this_channels_speaker(monkeypatch):
    """The device's own numbering, not the 0-based index: a recorder puts a
    different camera on each channel and each has a speaker of its own."""
    _patch_the_front_half(monkeypatch)
    client = _Client()

    await camera_module.DahuaCamera.async_play_audio(
        _camera(client, channel_number=7), "http://host/x.mp3", None
    )

    assert client.calls[0][3] == 7


async def test_no_format_asked_for_means_the_default_is_converted_to(monkeypatch):
    seen = _patch_the_front_half(monkeypatch)
    client = _Client()

    await camera_module.DahuaCamera.async_play_audio(
        _camera(client), "http://host/x.mp3", None
    )

    assert seen.seen[1] is AUDIO_FORMATS[DEFAULT_AUDIO_FORMAT]
    assert client.calls[0][1] == AUDIO_FORMATS[DEFAULT_AUDIO_FORMAT].content_type


async def test_a_device_that_declines_is_reported_rather_than_raising_raw(
    monkeypatch,
):
    """`DahuaWriteDeclined` is a ConnectionError, which reaches the service
    dialog as an unknown error instead of as what the device said."""
    _patch_the_front_half(monkeypatch)
    client = _Client(
        raises=camera_module.DahuaWriteDeclined("192.168.0.232 declined the audio")
    )

    with pytest.raises(HomeAssistantError) as raised:
        await camera_module.DahuaCamera.async_play_audio(
            _camera(client), "http://host/x.mp3", None
        )

    assert raised.value.translation_key == "audio_declined"
    assert "declined the audio" in raised.value.translation_placeholders["reason"]
    assert raised.value.translation_placeholders["device"] == "Front Door"


async def test_a_device_that_stops_taking_it_is_reported_too(monkeypatch):
    _patch_the_front_half(monkeypatch)
    client = _Client(raises=asyncio.TimeoutError("192.168.0.232 stopped taking audio"))

    with pytest.raises(HomeAssistantError) as raised:
        await camera_module.DahuaCamera.async_play_audio(
            _camera(client), "http://host/x.mp3", None
        )

    assert raised.value.translation_key == "audio_stopped"
    assert "stopped taking audio" in raised.value.translation_placeholders["reason"]


async def test_an_unknown_format_is_refused_before_anything_is_fetched(monkeypatch):
    """Cheapest failure first. Converting a file and then rejecting the format
    it was converted to would have the user wait for the wrong answer."""
    fetched = []

    async def fake_resolve(hass, media_content_id):
        fetched.append(media_content_id)
        return media_content_id

    monkeypatch.setattr(camera_module, "async_resolve_media", fake_resolve)

    with pytest.raises(HomeAssistantError):
        await camera_module.DahuaCamera.async_play_audio(
            _camera(_Client()), "http://host/x.mp3", "mp3"
        )

    assert fetched == []


# --- working out what was pointed at ----------------------------------------
#
# Every test above patches this out, so these are the only ones that run it.
# It is also where a Home Assistant signature is most likely to have moved
# under us, which a mock would hide rather than catch.


async def test_a_plain_url_is_handed_to_ffmpeg_as_it_stands(hass):
    assert (
        await async_resolve_media(hass, "http://example.com/chime.mp3")
        == "http://example.com/chime.mp3"
    )


async def test_a_url_relative_to_this_instance_is_made_absolute(hass):
    """What `tts.speak` leaves behind. ffmpeg has no idea what host to ask."""
    await async_setup_component(hass, "homeassistant", {})
    hass.config.internal_url = "http://192.168.0.210:8123"

    resolved = await async_resolve_media(hass, "/api/tts_proxy/abc123.mp3")

    assert resolved == "http://192.168.0.210:8123/api/tts_proxy/abc123.mp3"


async def test_a_media_source_id_is_resolved_to_its_url(hass, monkeypatch):
    """What the media selector in the service dialog produces.

    The resolving itself belongs to Home Assistant; what is asserted here is
    that this reaches for it at all, and passes on the URL it gets rather
    than the id.
    """
    from homeassistant.components import media_source

    async def fake_resolve(inner_hass, media_content_id, target):
        assert media_content_id == "media-source://tts/tts.google_en_com?message=hi"
        return media_source.models.PlayMedia("/api/tts_proxy/x.mp3", "audio/mpeg")

    monkeypatch.setattr(media_source, "async_resolve_media", fake_resolve)
    await async_setup_component(hass, "homeassistant", {})
    hass.config.internal_url = "http://192.168.0.210:8123"

    resolved = await async_resolve_media(
        hass, "media-source://tts/tts.google_en_com?message=hi"
    )

    # Resolved and then made absolute, because what comes back from a media
    # source is itself relative to this instance.
    assert resolved == "http://192.168.0.210:8123/api/tts_proxy/x.mp3"


# --- against a real ffmpeg --------------------------------------------------


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")
@pytest.mark.parametrize("name", sorted(AUDIO_FORMATS))
async def test_a_second_of_audio_really_is_byte_rate_bytes(
    hass, name, tmp_path, monkeypatch
):
    """The format table, checked against ffmpeg rather than against itself.

    Everything else here asserts that the right arguments were assembled.
    This is the only thing that says those arguments produce what the pacing
    assumes they produce, which is the claim the whole push rests on: one
    second of audio must be exactly `byte_rate` bytes, or the clip plays at
    the wrong speed on every device.

    Skipped where ffmpeg is missing, which includes some CI images.

    The binary is pointed at directly rather than through
    `get_ffmpeg_manager`, which needs the ffmpeg integration loaded and so
    needs `haffmpeg` installed. That is a dependency of Home Assistant's
    component, not of this one, and requiring it would make this test fail
    rather than skip on any image that ships the binary without it. What is
    being checked here is the arguments, not how the path is looked up.
    """
    monkeypatch.setattr(
        audio_module, "_ffmpeg_binary", lambda hass: shutil.which("ffmpeg")
    )
    audio_format = AUDIO_FORMATS[name]
    source = tmp_path / "tone.wav"
    generate = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=1000:duration=1",
        str(source),
    )
    await generate.wait()

    audio = await async_transcode(hass, str(source), audio_format)

    assert len(audio) == audio_format.byte_rate


async def test_a_file_outside_the_allowlist_is_refused(hass):
    """The same gate the backup services and camera snapshots use."""
    with pytest.raises(HomeAssistantError) as raised:
        await async_resolve_media(hass, "/etc/shadow")

    assert raised.value.translation_key == "audio_path_not_allowed"


async def test_an_ffmpeg_pseudo_protocol_does_not_pass_as_a_url(hass):
    """`concat:` and friends look like schemes and read local files.

    Allowing any string with a scheme in it would hand a service call the run
    of the disk through ffmpeg, so the schemes are named rather than the
    exclusions.
    """
    with pytest.raises(HomeAssistantError) as raised:
        await async_resolve_media(hass, "concat:/etc/passwd|/etc/shadow")

    assert raised.value.translation_key == "audio_path_not_allowed"


async def test_a_file_inside_the_allowlist_is_passed_straight_through(hass, tmp_path):
    chime = tmp_path / "chime.mp3"
    chime.write_bytes(b"not really an mp3")
    hass.config.allowlist_external_dirs = {str(tmp_path)}

    assert await async_resolve_media(hass, str(chime)) == str(chime)


async def test_an_rtsp_source_is_left_alone(hass):
    """A camera's own stream is a legitimate thing to announce from."""
    assert (
        await async_resolve_media(hass, "rtsp://192.168.0.232:554/live")
        == "rtsp://192.168.0.232:554/live"
    )
