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

from types import SimpleNamespace

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


def _coordinator(streams, monkeypatch, channel=11, address="10.0.0.5", port=None):
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
    if port is not None:
        # A real coordinator always has a client, and the card is keyed by the
        # device it names rather than by the address. `port=None` keeps the
        # older doubles that predate that and exercises the fallback.
        coordinator.client = SimpleNamespace(device_key="{0}:{1}".format(address, port))
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
    assert kwargs["translation_placeholders"]["channels"] == "11"
    assert kwargs["translation_placeholders"]["count"] == "1"


def test_the_card_can_be_acted_on(monkeypatch):
    """This was `is_fixable=False` while the card could only describe the fix
    in prose. It now offers to write H.264 to the channels the user picks, so
    the flag flips and the address has to travel with it: the flow is handed
    nothing but `data`, and it needs to know which host it is about.

    Only the address. The channel list is deliberately NOT carried, for the
    reason the leftover-rows card learned the hard way: a list in `data`
    outlives the evidence for it, and the flow recomputes at the button so a
    channel somebody fixed on the device is simply not offered."""
    coordinator, raised, _deleted = _coordinator(UNPLAYABLE, monkeypatch)

    coordinator._async_report_unplayable_stream()

    _issue_id, kwargs = raised[0]
    assert kwargs["is_fixable"] is True
    assert kwargs["data"]["address"] == "10.0.0.5"


def test_the_card_names_the_device_not_just_the_address(monkeypatch):
    """⭐ @rroller on #1127. Two Dahua boxes can sit behind one IP on different
    ports, and this is encoder state, which belongs to the device rather than
    to the network path. Keyed by address, the two shared one card and one
    affected-channel set: he had the repair write H.264 to the box on port
    8080 and clear the warning of the box on port 80, which was still sending
    H.265.

    The flow is handed nothing but `data`, so the device has to travel in it.
    """
    coordinator, raised, deleted = _coordinator(UNPLAYABLE, monkeypatch, port=80)

    coordinator._async_report_unplayable_stream()

    issue_id, kwargs = raised[0]
    assert issue_id == "webrtc_cannot_play_10.0.0.5:80"
    assert kwargs["data"] == {"address": "10.0.0.5", "device": "10.0.0.5:80"}
    assert "webrtc_cannot_play_10.0.0.5" in deleted, (
        "and the address-keyed card from #1048 goes, or an install that "
        "already has one keeps it for ever"
    )


def test_two_boxes_on_one_address_get_their_own_cards(monkeypatch):
    """The state behind the card, not just its id. One set per device, so one
    box's affected channels cannot be listed on the other's card."""
    first, raised, _deleted = _coordinator(UNPLAYABLE, monkeypatch, channel=0, port=80)
    first._async_report_unplayable_stream()
    second, raised_two, _d = _coordinator(UNPLAYABLE, monkeypatch, channel=5, port=8080)
    second._async_report_unplayable_stream()

    assert raised[0][0] == "webrtc_cannot_play_10.0.0.5:80"
    assert raised[0][1]["translation_placeholders"]["channels"] == "0"
    assert raised_two[-1][0] == "webrtc_cannot_play_10.0.0.5:8080"
    assert (
        raised_two[-1][1]["translation_placeholders"]["channels"] == "5"
    ), "channel 0 belongs to the other box and must not be listed here"


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


async def test_a_card_that_cannot_be_raised_does_not_fail_setup(monkeypatch):
    """The whole read is an enhancement to a diagnostic, and the card is the
    least important part of it. A registry that will not take the issue must
    cost the card and nothing else.

    Not hypothetical: calling the report outside the guard failed 40 tests in
    CI with "isn't fully initialized yet", because several build a coordinator
    whose `hass` is a stand-in that the issue registry cannot use. A real
    device would have lost its setup the same way.
    """
    from custom_components.dahua import coordinator as coordinator_module

    coordinator, _raised, _deleted = _coordinator(UNPLAYABLE, monkeypatch)

    def _explode(*args, **kwargs):
        raise AttributeError("hass has no issue registry")

    monkeypatch.setattr(coordinator_module.ir, "async_create_issue", _explode)

    async def _encode(_name):
        return {
            "table.Encode[11].MainFormat[0].Video.Compression": "H.265",
            "table.Encode[11].MainFormat[0].AudioEnable": "false",
        }

    coordinator.client = SimpleNamespace(async_get_config=_encode)

    await coordinator._async_read_stream_codecs()

    # The codecs still landed, which is what the camera attributes read.
    assert coordinator.get_stream_codec(0) == "H.265"
    assert coordinator.get_stream_has_audio(0) is False


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


# --- re-reading after a repair write -----------------------------------------
#
# ⭐ @rroller on #1127. The repair flow writes a codec and then has to ask the
# device what it actually sends now. The setup reader was reused for that, and
# it assigns whatever came back and re-judges the card from it. But
# `async_get_config` answers {} for an HTTP refusal, so a read-back that failed
# erased the known H.265 state and then DELETED the warning, having learned
# nothing about the write at all.
#
# These drive the real coordinator method rather than a flow double, because
# the first version of this was tested only through a double and three mutants
# of the real method survived.


def _rechecking(monkeypatch, answer, channel=11, raises=False):
    coordinator, raised, deleted = _coordinator(
        UNPLAYABLE, monkeypatch, channel=channel
    )

    async def _get_config(_name):
        if raises:
            raise RuntimeError("the device hung up")
        return answer

    coordinator.client = SimpleNamespace(
        async_get_config=_get_config, device_key="10.0.0.5:80"
    )
    return coordinator, raised, deleted


ANSWERED_H264 = {
    "table.Encode[11].MainFormat[0].Video.Compression": "H.264",
    "table.Encode[11].MainFormat[0].AudioEnable": "false",
}


async def test_a_real_answer_updates_the_codecs_and_says_so(monkeypatch):
    coordinator, _raised, deleted = _rechecking(monkeypatch, ANSWERED_H264)

    assert await coordinator.async_recheck_stream_codecs() is True
    assert coordinator.get_stream_codec(0) == "H.264"
    assert deleted, "H.264 is playable, so the card goes"


async def test_an_empty_answer_keeps_what_is_held(monkeypatch):
    """⭐ The reported case. {} is what `async_get_config` returns for a
    refusal, and it is indistinguishable from a device with nothing to say."""
    coordinator, _raised, deleted = _rechecking(monkeypatch, {})

    assert await coordinator.async_recheck_stream_codecs() is False
    assert coordinator.get_stream_codec(0) == "H.265", "not erased"
    assert deleted == [], "and the warning is not cleared on an erasure"


async def test_an_answer_about_another_channel_is_not_this_ones(monkeypatch):
    """Encode is one table for the whole device. A reply carrying another
    channel's rows is nonempty and still says nothing about this channel."""
    coordinator, _raised, deleted = _rechecking(
        monkeypatch,
        {"table.Encode[3].MainFormat[0].Video.Compression": "H.264"},
    )

    assert await coordinator.async_recheck_stream_codecs() is False
    assert coordinator.get_stream_codec(0) == "H.265"
    assert deleted == []


async def test_a_read_that_raises_is_not_a_confirmation(monkeypatch):
    coordinator, _raised, deleted = _rechecking(monkeypatch, None, raises=True)

    assert await coordinator.async_recheck_stream_codecs() is False
    assert coordinator.get_stream_codec(0) == "H.265"
    assert deleted == []


def test_the_affected_channels_are_read_per_device(monkeypatch):
    """`channels_that_cannot_be_played` is what the repair flow asks for the
    list of channels to offer. Keyed by address it answered with the other
    box's channels too, which is the same mix-up one layer down."""
    first, _raised, _deleted = _coordinator(UNPLAYABLE, monkeypatch, channel=0, port=80)
    first._async_report_unplayable_stream()
    second, _r, _d = _coordinator(UNPLAYABLE, monkeypatch, channel=5, port=8080)
    second._async_report_unplayable_stream()

    assert first.channels_that_cannot_be_played() == {0}
    assert second.channels_that_cannot_be_played() == {5}


# --- a partial answer is not an answer about the codec -----------------------
#
# ⭐ @rroller on #1127, after the empty-answer guard went in. "Nonempty" is one
# question short of "said something about this stream's codec".
# `parse_stream_codecs` seeds every field it did not see with None, so both of
# the replies below produce a nonempty map whose main stream has codec None.
# Assigning it erased the H.265 and let the card be re-judged on the erasure,
# so the warning went while the flow was reporting the write refused.
#
# This is the same shape as the partial-answer finding on #997, one level up:
# there it was the poll, here it is the repair's read back.


DEVICE_CARD = "webrtc_cannot_play_10.0.0.5:80"


def _card_was_cleared(deleted):
    """Only the device's own card counts.

    `_async_report_unplayable_stream` also deletes the address-keyed id from
    #1048 every time it runs, which is the migration cleanup rather than the
    warning being withdrawn. Counting deletions conflated the two.
    """
    return DEVICE_CARD in deleted


AUDIO_ONLY = {"table.Encode[11].MainFormat[0].AudioEnable": "false"}
SUB_STREAM_ONLY = {"table.Encode[11].ExtraFormat[0].Video.Compression": "H.264"}


async def test_an_audio_only_answer_does_not_erase_the_codec(monkeypatch):
    coordinator, _raised, deleted = _rechecking(monkeypatch, AUDIO_ONLY)

    assert await coordinator.async_recheck_stream_codecs() is False
    assert coordinator.get_stream_codec(0) == "H.265", "not erased"
    assert not _card_was_cleared(deleted), "and the warning stands"


async def test_a_sub_stream_answer_does_not_erase_the_main_one(monkeypatch):
    coordinator, _raised, deleted = _rechecking(monkeypatch, SUB_STREAM_ONLY)

    assert await coordinator.async_recheck_stream_codecs() is False
    assert coordinator.get_stream_codec(0) == "H.265"
    assert not _card_was_cleared(deleted)


async def test_a_partial_answer_still_applies_what_it_did_say(monkeypatch):
    """The merge must not become "ignore partial answers". A sub stream that
    really did move is how the rest of the integration notices."""
    coordinator, _raised, _deleted = _rechecking(monkeypatch, SUB_STREAM_ONLY)

    await coordinator.async_recheck_stream_codecs()

    assert coordinator.get_stream_codec(1) == "H.264", "applied where specific"
    assert coordinator.get_stream_codec(0) == "H.265", "untouched where silent"


async def test_an_audio_flag_moving_is_applied_but_not_a_confirmation(monkeypatch):
    """Both halves in one. The flag lands, because the answer stated it, and
    the return is still False, because nothing was said about the codec."""
    coordinator, _raised, _deleted = _rechecking(
        monkeypatch, {"table.Encode[11].MainFormat[0].AudioEnable": "true"}
    )

    assert await coordinator.async_recheck_stream_codecs() is False
    assert coordinator.get_stream_has_audio(0) is True
    assert coordinator.get_stream_codec(0) == "H.265"


async def test_a_full_answer_is_still_a_confirmation(monkeypatch):
    """The converse, so none of the above degenerates into "never confirm"."""
    coordinator, _raised, deleted = _rechecking(monkeypatch, ANSWERED_H264)

    assert await coordinator.async_recheck_stream_codecs() is True
    assert coordinator.get_stream_codec(0) == "H.264"
    assert _card_was_cleared(deleted)
