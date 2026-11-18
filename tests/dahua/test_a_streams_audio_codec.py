"""Which audio a stream carries, on the camera entity (#1162).

The camera entity already says `video_codec` and whether `audio` is on. What it
did not say is which audio codec and at what sample rate, and that is the half
that decides whether Home Assistant's talk button can play anything.

@MrCheng888 measured it on a VTO3211D on firmware 4.400. Out of the box the main
stream's audio was PCM at 16000 Hz and the talk button produced silence; writing

    Encode[0].MainFormat[0].Audio.Compression=G.711A
    Encode[0].MainFormat[0].Audio.Frequency=8000

fixed it, and on that firmware the setting goes back to PCM 16000 on every
reboot of the door station. So the values here are his, and the case that
matters most is the one where the codec changes underneath a running entry.

No request is added. Both fields come from the Encode answer the codecs already
come from: once at setup, and again on any poll that reads Encode.
"""

import pytest

from custom_components.dahua.coordinator import DahuaDataUpdateCoordinator
from custom_components.dahua.dahua_utils import parse_stream_codecs

MAIN = "table.Encode[{0}].MainFormat[0]"
EXTRA = "table.Encode[{0}].ExtraFormat[0]"

# The VTO3211D as shipped, and after @MrCheng888's write.
AS_SHIPPED = ("PCM", "16000")
FIXED = ("G.711A", "8000")


def _encode(channel=0, main=FIXED, extra=None, video="H.264", audio="true"):
    table = {
        MAIN.format(channel) + ".Video.Compression": video,
        MAIN.format(channel) + ".AudioEnable": audio,
        MAIN.format(channel) + ".Audio.Compression": main[0],
        MAIN.format(channel) + ".Audio.Frequency": main[1],
    }
    if extra is not None:
        table[EXTRA.format(channel) + ".Audio.Compression"] = extra[0]
        table[EXTRA.format(channel) + ".Audio.Frequency"] = extra[1]
    return table


# --- the parser ---------------------------------------------------------------


def test_the_audio_codec_and_rate_are_read_for_each_stream():
    found = parse_stream_codecs(_encode(main=AS_SHIPPED, extra=FIXED), 0)

    assert found[0]["audio_codec"] == "PCM"
    assert found[0]["audio_rate"] == 16000
    assert found[1]["audio_codec"] == "G.711A"
    assert found[1]["audio_rate"] == 8000


def test_a_stream_with_nothing_said_about_audio_looks_as_it_did():
    """Added only when present rather than seeded with None, so every caller
    that compares a stream's record whole sees exactly what it saw before."""
    table = {
        MAIN.format(0) + ".Video.Compression": "H.265",
        MAIN.format(0) + ".AudioEnable": "false",
    }

    assert parse_stream_codecs(table, 0) == {0: {"codec": "H.265", "audio": False}}


@pytest.mark.parametrize("rate", ["", "unknown", "16k"])
def test_a_rate_that_is_not_a_number_is_left_out(rate):
    found = parse_stream_codecs(_encode(main=("PCM", rate)), 0)

    assert "audio_rate" not in found[0]
    assert found[0]["audio_codec"] == "PCM"


def test_a_rate_sent_as_a_decimal_is_read():
    """Robustness, not a measurement. Dahua sends some numbers in the decimal
    form, the storage read's byte counts among them; nobody has seen this field
    sent that way, but if it is, it is still a whole number of hertz."""
    found = parse_stream_codecs(_encode(main=("PCM", "16000.000000")), 0)

    assert found[0]["audio_rate"] == 16000


def test_a_blank_codec_is_left_out():
    found = parse_stream_codecs(_encode(main=("  ", "8000")), 0)

    assert "audio_codec" not in found[0]


def test_another_channels_audio_is_not_taken():
    table = _encode(channel=3, main=AS_SHIPPED)

    assert parse_stream_codecs(table, 0) == {}


# --- kept current from the poll ----------------------------------------------


def _coordinator(held=None):
    c = object.__new__(DahuaDataUpdateCoordinator)
    c._channel = 0
    c._address = "10.0.0.5"
    c._stream_codecs = held if held is not None else {}
    c._async_report_unplayable_stream = lambda: None
    return c


def test_a_codec_that_reverts_after_a_reboot_is_seen_on_the_next_read():
    """The case that matters. Set to G.711A, the door station reboots, and the
    next Encode answer says PCM again. Without this the camera would go on
    claiming the codec that was set, until a reload."""
    c = _coordinator()
    c._async_refresh_stream_codecs(_encode(main=FIXED))
    assert c.get_stream_audio_codec(0) == "G.711A"

    c._async_refresh_stream_codecs(_encode(main=AS_SHIPPED))

    assert c.get_stream_audio_codec(0) == "PCM"
    assert c.get_stream_audio_rate(0) == 16000


def test_an_answer_that_says_nothing_about_audio_keeps_what_is_held():
    """The per-field merge #997 established, applied to the two new fields: a
    reply carrying the video codec and not the audio one must not erase it."""
    c = _coordinator(
        {
            0: {
                "codec": "H.264",
                "audio": True,
                "audio_codec": "G.711A",
                "audio_rate": 8000,
            }
        }
    )

    c._async_refresh_stream_codecs(
        {
            MAIN.format(0) + ".Video.Compression": "H.264",
            MAIN.format(0) + ".AudioEnable": "true",
        }
    )

    assert c.get_stream_audio_codec(0) == "G.711A"
    assert c.get_stream_audio_rate(0) == 8000


def test_nothing_known_reads_as_none():
    c = _coordinator()

    assert c.get_stream_audio_codec(0) is None
    assert c.get_stream_audio_rate(0) is None


# --- and the camera shows it ---------------------------------------------------


def _camera(audio_codec, audio_rate):
    from custom_components.dahua.camera import DahuaCamera

    camera = object.__new__(DahuaCamera)
    camera._stream_index = 0
    camera._coordinator = _coordinator(
        {
            0: {
                "codec": "H.264",
                "audio": True,
                "audio_codec": audio_codec,
                "audio_rate": audio_rate,
            }
        }
    )
    camera._coordinator.data = {"id": "SERIAL1"}
    camera.coordinator = camera._coordinator
    return camera


def test_the_camera_says_which_audio_it_carries():
    """Through the real coordinator getters, so a camera reading the wrong key
    fails here rather than passing on a double's idea of the answer."""
    attributes = _camera("PCM", 16000).extra_state_attributes

    assert attributes["audio_codec"] == "PCM"
    assert attributes["audio_sample_rate"] == 16000
    assert attributes["audio"] is True


def test_a_camera_whose_audio_was_not_said_grows_nothing():
    attributes = _camera(None, None).extra_state_attributes

    assert "audio_codec" not in attributes
    assert "audio_sample_rate" not in attributes
