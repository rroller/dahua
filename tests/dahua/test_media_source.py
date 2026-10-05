"""Browsing and playing a recorder's recordings (#472).

The tree is camera -> day -> clip; resolving a clip builds the RTSP cam/playback
url and hands it to Home Assistant's stream component for HLS. The device calls
are faked, and create_stream is patched, so these exercise the platform's own
decisions: the tree it builds, the identifier it round-trips, the channel it
plays, and that the credentialled url never reaches a title or identifier.
"""

import types

import pytest

from custom_components.dahua import media_source as ms
from custom_components.dahua.media_source import (
    DAYS_LISTED,
    DahuaMediaSource,
    _clip_title,
    _to_underscore,
)

R1 = {
    "StartTime": "2026-10-04 12:00:00",
    "EndTime": "2026-10-04 13:00:00",
    "FilePath": "/mnt/dvr/a.dav",
    "Flags": ["Timing"],
}
R2 = {
    "StartTime": "2026-10-04 13:00:00",
    "EndTime": "2026-10-04 14:00:00",
    "FilePath": "/mnt/dvr/b.dav",
    "Flags": ["Event"],
}

SECRET = "SECRETPASS"


class _Client:
    def __init__(self, records, channel_number):
        self._records = records
        self._channel_number = channel_number
        self.find_calls = []

    async def async_find_recordings(self, channel, start, end, max_results=200):
        self.find_calls.append((channel, start, end))
        return self._records

    def get_rtsp_playback_url(self, channel_number, start, end):
        return "rtsp://u:%s@h:554/cam/playback?channel=%s&starttime=%s&endtime=%s" % (
            SECRET,
            channel_number,
            start,
            end,
        )


class _Coord:
    def __init__(
        self,
        channel=0,
        name="Front Door",
        no_video=False,
        records=None,
        channel_number=None,
    ):
        self._channel = channel
        self._name = name
        self._no_video = no_video
        # Default to something other than channel+1 so a test can prove the
        # resolve plays get_channel_number(), not a blind +1.
        self._channel_number = 1 if channel_number is None else channel_number
        self.client = _Client(records or [], self._channel_number)

    def is_indoor_monitor_without_video(self):
        return self._no_video

    def get_channel(self):
        return self._channel

    def get_device_name(self):
        return self._name

    def get_channel_number(self):
        return self._channel_number


class _Entry:
    def __init__(self, entry_id, coords):
        self.entry_id = entry_id
        self.runtime_data = {c.get_channel(): c for c in coords}


class _ConfigEntries:
    def __init__(self, entries):
        self._entries = {e.entry_id: e for e in entries}

    def async_loaded_entries(self, domain):
        return list(self._entries.values())

    def async_get_entry(self, entry_id):
        return self._entries.get(entry_id)


class _Hass:
    def __init__(self, *entries):
        self.config_entries = _ConfigEntries(entries)


def _item(identifier):
    return types.SimpleNamespace(identifier=identifier)


# --- pure helpers -----------------------------------------------------------


def test_to_underscore_is_the_cam_playback_time_form():
    assert _to_underscore("2026-10-04 12:00:00") == "2026_10_04_12_00_00"


def test_clip_title_names_the_span_and_the_kind():
    assert _clip_title(R1) == "12:00:00 - 13:00:00  Continuous"
    assert _clip_title(R2) == "13:00:00 - 14:00:00  Event"
    assert _clip_title({"StartTime": "d 01:00:00", "EndTime": "d 02:00:00"}) == (
        "01:00:00 - 02:00:00"
    )


# --- browse -----------------------------------------------------------------


async def test_root_lists_cameras_and_skips_an_indoor_monitor():
    entry = _Entry(
        "e1",
        [
            _Coord(channel=0, name="Front Door"),
            _Coord(channel=1, name="VTH", no_video=True),
        ],
    )
    source = DahuaMediaSource(_Hass(entry))

    root = await source.async_browse_media(_item(None))

    assert [child.title for child in root.children] == ["Front Door"]
    assert root.children[0].identifier == "CAM|e1|0"
    assert root.children[0].can_expand is True
    assert root.children[0].can_play is False


async def test_a_camera_lists_the_last_days_newest_first_without_querying():
    coord = _Coord(channel=0)
    source = DahuaMediaSource(_Hass(_Entry("e1", [coord])))

    days = await source.async_browse_media(_item("CAM|e1|0"))

    assert len(days.children) == DAYS_LISTED
    # Listed blind: not a single find was made to build the day list.
    assert coord.client.find_calls == []
    titles = [child.title for child in days.children]
    assert titles == sorted(titles, reverse=True)  # newest day first
    assert days.children[0].identifier == "DAY|e1|0|%s" % titles[0]


async def test_a_day_lists_clips_from_the_finder():
    coord = _Coord(channel=0, records=[R1, R2])
    source = DahuaMediaSource(_Hass(_Entry("e1", [coord])))

    clips = await source.async_browse_media(_item("DAY|e1|0|2026-10-04"))

    assert coord.client.find_calls == [
        (0, "2026-10-04 00:00:00", "2026-10-04 23:59:59")
    ]
    assert [child.title for child in clips.children] == [
        "12:00:00 - 13:00:00  Continuous",
        "13:00:00 - 14:00:00  Event",
    ]
    first = clips.children[0]
    assert first.identifier == "FILE|e1|0|2026_10_04_12_00_00|2026_10_04_13_00_00"
    assert first.can_play is True
    assert first.can_expand is False


async def test_a_day_is_cached_so_a_re_browse_does_not_re_query():
    coord = _Coord(channel=0, records=[R1])
    source = DahuaMediaSource(_Hass(_Entry("e1", [coord])))

    await source.async_browse_media(_item("DAY|e1|0|2026-10-04"))
    await source.async_browse_media(_item("DAY|e1|0|2026-10-04"))

    assert len(coord.client.find_calls) == 1


async def test_no_credential_reaches_a_browse_title_or_identifier():
    coord = _Coord(channel=0, records=[R1, R2])
    source = DahuaMediaSource(_Hass(_Entry("e1", [coord])))

    root = await source.async_browse_media(_item(None))
    days = await source.async_browse_media(_item("CAM|e1|0"))
    clips = await source.async_browse_media(_item("DAY|e1|0|2026-10-04"))

    for node in (root, days, clips):
        for child in node.children:
            assert SECRET not in (child.identifier or "")
            assert SECRET not in child.title


# --- resolve ----------------------------------------------------------------


def _patch_stream(monkeypatch):
    """Replace create_stream with a recorder and return it."""
    captured = {}

    class _Stream:
        def add_provider(self, fmt, timeout=None):
            captured["provider"] = (fmt, timeout)

        def endpoint_url(self, fmt):
            captured["endpoint_fmt"] = fmt
            return "/api/hls/TOKEN/master_playlist.m3u8"

    def fake_create_stream(hass, url, options, settings):
        captured["url"] = url
        captured["options"] = options
        return _Stream()

    monkeypatch.setattr(ms, "create_stream", fake_create_stream)
    monkeypatch.setattr(ms, "DynamicStreamSettings", lambda: object())
    return captured


async def test_resolving_a_clip_plays_the_channel_number_over_hls(monkeypatch):
    captured = _patch_stream(monkeypatch)
    # channel index 0 but detected channel number 5: a blind +1 would play the
    # wrong camera, which is the regression the auto-detect option exists for.
    coord = _Coord(channel=0, channel_number=5)
    source = DahuaMediaSource(_Hass(_Entry("e1", [coord])))

    media = await source.async_resolve_media(
        _item("FILE|e1|0|2026_10_04_12_00_00|2026_10_04_13_00_00")
    )

    assert "channel=5" in captured["url"]
    assert "starttime=2026_10_04_12_00_00" in captured["url"]
    assert "endtime=2026_10_04_13_00_00" in captured["url"]
    assert captured["provider"] == ("hls", 3600)
    assert captured["endpoint_fmt"] == "hls"
    # The master prefix is stripped, the way Reolink's media source does it.
    assert media.url == "/api/hls/TOKEN/playlist.m3u8"
    # The exact string the media-browser dialog matches to use its hls.js player;
    # the other HLS spelling falls back to a <video> that Chromium cannot play.
    assert media.mime_type == "application/x-mpegURL"


async def test_resolve_rejects_a_non_file_identifier(monkeypatch):
    _patch_stream(monkeypatch)
    source = DahuaMediaSource(_Hass(_Entry("e1", [_Coord()])))

    from homeassistant.components.media_source import Unresolvable

    with pytest.raises(Unresolvable):
        await source.async_resolve_media(_item("DAY|e1|0|2026-10-04"))


async def test_resolve_on_an_unloaded_device_is_unresolvable(monkeypatch):
    _patch_stream(monkeypatch)
    source = DahuaMediaSource(_Hass())  # no entries loaded

    from homeassistant.components.media_source import Unresolvable

    with pytest.raises(Unresolvable):
        await source.async_resolve_media(
            _item("FILE|gone|0|2026_10_04_12_00_00|2026_10_04_13_00_00")
        )
