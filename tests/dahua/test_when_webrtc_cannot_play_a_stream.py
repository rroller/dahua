"""H.265 with no audio track cannot be played, and now says so three ways.

#1036 put the codec in the diagnostics dump and deliberately raised no warning,
because H.265 on its own is not universally broken: it depends on the player,
and the same recorder serves a working H.264 sub stream beside an unplayable
H.265 main one. "Your stream will not work" would have been wrong often enough
to be worse than silence.

This is the narrower condition, and it is not a matter of taste. Measured on a
DHI-NVR5464-16P-EI, channel 11, H.265 on every stream with AudioEnable false:

    go2rtc dahua_..._11_Main          200 OK   m=video H265
    go2rtc dahua_..._11_Main?audio    404 Not Found
    a browser-shaped WebRTC offer     500, naming that 404

A browser cannot take H.265 over WebRTC, so go2rtc falls back to the producer
Home Assistant registers as `ffmpeg:<stream>#audio=opus`, and that producer asks
go2rtc for the stream's audio-only view. There is no audio track, so it 404s and
the whole attempt dies. Moving that channel to H.264 turned the same offer into
a 200 carrying video with the audio line marked inactive, and real frames came
out of it. Nothing else changed: same resolution, frame rate and bitrate.

So the pair is reported, never the codec alone.
"""

import pytest

from custom_components.dahua.dahua_utils import (
    parse_stream_codecs,
    webrtc_cannot_play,
)

MEASURED = {
    "table.Encode[11].MainFormat[0].Video.Compression": "H.265",
    "table.Encode[11].MainFormat[0].AudioEnable": "false",
    "table.Encode[11].ExtraFormat[0].Video.Compression": "H.265",
    "table.Encode[11].ExtraFormat[0].AudioEnable": "false",
    "table.Encode[11].ExtraFormat[1].Video.Compression": "H.264",
    "table.Encode[11].ExtraFormat[1].AudioEnable": "true",
    # Another channel, which must not leak into channel 11's answer.
    "table.Encode[0].MainFormat[0].Video.Compression": "H.264",
    "table.Encode[0].MainFormat[0].AudioEnable": "true",
}


# --- reading the table -------------------------------------------------------


def test_each_stream_is_read():
    streams = parse_stream_codecs(MEASURED, 11)

    assert streams[0] == {"codec": "H.265", "audio": False}
    assert streams[1] == {"codec": "H.265", "audio": False}
    assert streams[2] == {"codec": "H.264", "audio": True}


def test_the_indexes_are_the_ones_the_integration_uses():
    """MainFormat[0] is stream 0 and ExtraFormat[n] is stream n+1, which is what
    `to_stream_name` and every camera entity already use. A camera looks up its
    own stream by its own index and must not have to translate anything."""
    streams = parse_stream_codecs(MEASURED, 11)

    assert sorted(streams) == [0, 1, 2]


def test_another_channels_streams_are_not_mixed_in():
    """Encode is one table for the whole device, indexed by channel, and the
    channels of a recorder share the read."""
    streams = parse_stream_codecs(MEASURED, 0)

    assert streams == {0: {"codec": "H.264", "audio": True}}


@pytest.mark.parametrize(
    "encode",
    [None, "Error", {}, {"nothing": "useful"}],
    ids=["none", "str", "{}", "other keys"],
)
def test_a_table_that_says_nothing_is_nothing(encode):
    assert parse_stream_codecs(encode, 11) == {}


# --- and judging it ----------------------------------------------------------


def test_h265_with_no_audio_cannot_be_played():
    assert webrtc_cannot_play(parse_stream_codecs(MEASURED, 11)) is True


def test_the_sub_stream_is_judged_separately():
    """Stream 2 of that channel is H.264. The same channel can carry a stream
    that plays and one that does not, which is why this takes an index."""
    streams = parse_stream_codecs(MEASURED, 11)

    assert webrtc_cannot_play(streams, 1) is True
    assert webrtc_cannot_play(streams, 2) is False


def test_h265_with_audio_is_not_reported():
    """The restraint #1036 chose, kept. This depends on the player: go2rtc can
    transcode it, because there is an audio track for the converter to work
    with. Saying it will not play would be wrong."""
    streams = parse_stream_codecs(
        {
            "table.Encode[0].MainFormat[0].Video.Compression": "H.265",
            "table.Encode[0].MainFormat[0].AudioEnable": "true",
        },
        0,
    )

    assert webrtc_cannot_play(streams) is False


def test_h264_with_no_audio_is_not_reported():
    """Which is most cameras here. The browser takes the video directly and the
    audio-only view is never asked for."""
    streams = parse_stream_codecs(
        {
            "table.Encode[0].MainFormat[0].Video.Compression": "H.264",
            "table.Encode[0].MainFormat[0].AudioEnable": "false",
        },
        0,
    )

    assert webrtc_cannot_play(streams) is False


def test_a_stream_the_device_said_nothing_about_is_not_reported():
    """An unread table must not raise a card. Neither half is known, and
    guessing from silence is how a warning becomes noise."""
    assert webrtc_cannot_play({}) is False
    assert webrtc_cannot_play({0: {"codec": None, "audio": None}}) is False
    assert webrtc_cannot_play({0: {"codec": "H.265", "audio": None}}) is False


# --- the card ----------------------------------------------------------------


def _coordinator(streams, monkeypatch, channel=11, address="10.0.0.5"):
    from custom_components.dahua import coordinator as coordinator_module

    raised, deleted = [], []
    monkeypatch.setattr(
        coordinator_module.ir,
        "async_create_issue",
        lambda hass, domain, issue_id, **kwargs: raised.append((issue_id, kwargs)),
    )
    monkeypatch.setattr(
        coordinator_module.ir,
        "async_delete_issue",
        lambda hass, domain, issue_id: deleted.append(issue_id),
    )

    coordinator = object.__new__(coordinator_module.DahuaDataUpdateCoordinator)
    coordinator.hass = object()
    coordinator._address = address
    coordinator._channel = channel
    coordinator._stream_codecs = streams
    coordinator.get_device_name = lambda: "CCTV in Server Room"
    return coordinator, raised, deleted


@pytest.fixture(autouse=True)
def _forget_which_channels_were_affected():
    """Module state, keyed by host, and the suite runs under -n auto."""
    from custom_components.dahua import coordinator as coordinator_module

    coordinator_module._HOST_UNPLAYABLE.clear()
    yield
    coordinator_module._HOST_UNPLAYABLE.clear()


UNPLAYABLE = {0: {"codec": "H.265", "audio": False}}
PLAYABLE = {0: {"codec": "H.264", "audio": False}}


def test_one_card_names_the_channel(monkeypatch):
    coordinator, raised, _deleted = _coordinator(UNPLAYABLE, monkeypatch)

    coordinator._async_report_unplayable_stream()

    assert len(raised) == 1
    issue_id, kwargs = raised[0]
    assert issue_id == "webrtc_cannot_play_10.0.0.5"
    assert kwargs["translation_key"] == "webrtc_cannot_play"
    assert kwargs["is_fixable"] is False
    assert kwargs["translation_placeholders"]["channels"] == "11"
    assert kwargs["translation_placeholders"]["count"] == "1"


def test_eight_affected_channels_are_one_card(monkeypatch):
    """The measurement that decided this shape. Eight of the twelve channels on
    the recorder this was built against carry H.265 with no audio, and eight
    cards about one device is a pile rather than a message."""
    raised = []
    for channel in range(2, 10):
        coordinator, raised, _deleted = _coordinator(
            UNPLAYABLE, monkeypatch, channel=channel
        )
        coordinator._async_report_unplayable_stream()

    issue_ids = {issue_id for issue_id, _kwargs in raised}
    assert issue_ids == {"webrtc_cannot_play_10.0.0.5"}, issue_ids
    assert raised[-1][1]["translation_placeholders"]["channels"] == (
        "2, 3, 4, 5, 6, 7, 8, 9"
    )
    assert raised[-1][1]["translation_placeholders"]["count"] == "8"


def test_two_recorders_do_not_share_a_card(monkeypatch):
    """Keyed by address, like everything else here that is per host."""
    first, raised, _d = _coordinator(UNPLAYABLE, monkeypatch, address="10.0.0.5")
    first._async_report_unplayable_stream()
    second, raised_two, _d2 = _coordinator(UNPLAYABLE, monkeypatch, address="10.0.0.9")
    second._async_report_unplayable_stream()

    assert raised_two[-1][0] == "webrtc_cannot_play_10.0.0.9"
    assert raised_two[-1][1]["translation_placeholders"]["count"] == "1"


def test_a_channel_that_was_fixed_leaves_the_card(monkeypatch):
    """Two bad channels, one of them changed to H.264 and reloaded. The card
    stays for the other one and stops naming the one that was fixed."""
    for channel in (2, 3):
        coordinator, _r, _d = _coordinator(UNPLAYABLE, monkeypatch, channel=channel)
        coordinator._async_report_unplayable_stream()

    fixed, raised, _deleted = _coordinator(PLAYABLE, monkeypatch, channel=3)
    fixed._async_report_unplayable_stream()

    assert raised[-1][1]["translation_placeholders"]["channels"] == "2"


def test_the_last_fixed_channel_clears_the_card(monkeypatch):
    coordinator, _r, _d = _coordinator(UNPLAYABLE, monkeypatch)
    coordinator._async_report_unplayable_stream()

    fixed, raised, deleted = _coordinator(PLAYABLE, monkeypatch)
    fixed._async_report_unplayable_stream()

    assert raised == []
    assert deleted == ["webrtc_cannot_play_10.0.0.5"]


def test_a_device_that_said_nothing_raises_nothing(monkeypatch):
    coordinator, raised, _deleted = _coordinator({}, monkeypatch)

    coordinator._async_report_unplayable_stream()

    assert raised == []


# --- and the camera says what it carries -------------------------------------


def _camera(codec, audio, stream_index=0):
    from custom_components.dahua.camera import DahuaCamera

    camera = object.__new__(DahuaCamera)
    camera._stream_index = stream_index
    camera._coordinator = type(
        "_C",
        (),
        {
            "get_stream_codec": lambda self, index: codec,
            "get_stream_has_audio": lambda self, index: audio,
            "data": {"id": "SERIAL1"},
        },
    )()
    camera.coordinator = camera._coordinator
    return camera


def test_the_camera_says_which_codec_it_carries():
    """On the camera rather than a sensor of its own: a recorder already has one
    camera entity per stream, and twelve channels would otherwise mean twelve
    more entities to describe streams that are right there."""
    attributes = _camera("H.265", False).extra_state_attributes

    assert attributes["video_codec"] == "H.265"
    assert attributes["audio"] is False


def test_the_base_attributes_survive():
    attributes = _camera("H.264", True).extra_state_attributes

    assert attributes["integration"] == "dahua"
    assert attributes["audio"] is True


def test_a_camera_whose_table_was_refused_grows_nothing():
    attributes = _camera(None, None).extra_state_attributes

    assert "video_codec" not in attributes
    assert "audio" not in attributes
    assert attributes["integration"] == "dahua"
