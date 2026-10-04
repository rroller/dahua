"""Expose a Dahua recorder's recorded clips as a Home Assistant media source.

Browse by camera, then by day, then pick a clip; playing one resolves to the
device's RTSP cam/playback VOD, which Home Assistant's stream component turns
into HLS the frontend can play -- the same path Reolink's media source uses.

The parity gap this closes (#472): Reolink and Tapo browse recordings in Media,
this integration did not. Measured on a DHI-NVR5464: mediaFileFind lists hourly
.dav files per channel, and cam/playback serves each as a finite VOD.

Limitations, stated rather than discovered:
- Days are listed blind (the last DAYS_LISTED calendar days). Dahua has no cheap
  "which days have footage" call, so an empty day shows an empty folder rather
  than being hidden, the way Reolink hides it with a status-only query.
- Times are the recorder's own local clock. The clip identifier carries the
  device's exact timestamps, so playback is unaffected, but the day boundaries
  used to list a day follow whatever clock the NVR keeps.
"""

import logging
import time
from datetime import timedelta

from homeassistant.components.camera import DynamicStreamSettings
from homeassistant.components.media_player import MediaClass, MediaType
from homeassistant.components.media_source import (
    BrowseMediaSource,
    MediaSource,
    MediaSourceItem,
    PlayMedia,
    Unresolvable,
)
from homeassistant.components.stream import (
    FORMAT_CONTENT_TYPE,
    HLS_PROVIDER,
    create_stream,
)
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.dahua import entry_coordinators

from .const import DOMAIN

_LOGGER = logging.getLogger(__package__)

# How many calendar days back to offer. Listed blind, so this is a browse depth,
# not a query cost: no day is queried until the user opens it.
DAYS_LISTED = 14

# A browse of a day costs a round trip to the recorder; the media browser
# re-browses a level more than once, so a short cache spares the device.
_CLIP_CACHE_TTL = 30.0

# Dahua recording flags -> a word a person recognises in the browser.
_FLAG_LABELS = {
    "Timing": "Continuous",
    "Event": "Event",
    "Manual": "Manual",
    "Marker": "Marked",
}


def _to_underscore(dahua_time: str) -> str:
    """ "2026-10-04 12:00:00" -> "2026_10_04_12_00_00", the cam/playback form."""
    return dahua_time.replace("-", "_").replace(" ", "_").replace(":", "_")


def _clip_title(record: dict) -> str:
    """A clip's label: its time span, then the kind of recording it is."""
    start = str(record.get("StartTime", "") or "")
    end = str(record.get("EndTime", "") or "")
    start_clock = start.split(" ", 1)[1] if " " in start else start
    end_clock = end.split(" ", 1)[1] if " " in end else end
    label = "/".join(
        _FLAG_LABELS[flag]
        for flag in (record.get("Flags") or [])
        if flag in _FLAG_LABELS
    )
    span = "{0} - {1}".format(start_clock, end_clock).strip(" -")
    return "{0}  {1}".format(span, label).strip() if label else span


async def async_get_media_source(hass: HomeAssistant) -> "DahuaMediaSource":
    """Set up Dahua media source."""
    return DahuaMediaSource(hass)


class DahuaMediaSource(MediaSource):
    """Browse and play a Dahua recorder's recordings."""

    name: str = "Dahua"

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the Dahua media source."""
        super().__init__(DOMAIN)
        self.hass = hass
        self._clip_cache: dict[tuple[str, int, str], tuple[float, list[dict]]] = {}

    # -- resolve ------------------------------------------------------------

    async def async_resolve_media(self, item: MediaSourceItem) -> PlayMedia:
        """Turn a chosen clip into a playable HLS url."""
        parts = (item.identifier or "").split("|")
        if len(parts) != 5 or parts[0] != "FILE":
            raise Unresolvable("Unknown Dahua media item '{0}'".format(item.identifier))
        _, entry_id, channel_str, start, end = parts

        coordinator = self._coordinator(entry_id, int(channel_str))
        if coordinator is None:
            raise Unresolvable(
                "Dahua device for '{0}' is not loaded".format(item.identifier)
            )

        # The url carries the credentials, so it goes to create_stream and
        # nowhere else -- never a log line, a title, or the identifier.
        url = coordinator.client.get_rtsp_playback_url(
            coordinator.get_channel_number(), start, end
        )
        stream = create_stream(self.hass, url, {}, DynamicStreamSettings())
        stream.add_provider(HLS_PROVIDER, timeout=3600)
        # Hand back the media playlist, not the master. Reolink's platinum media
        # source does the same replace; it is a no-op if the endpoint name has no
        # "master_" prefix, so it can only help.
        endpoint = stream.endpoint_url(HLS_PROVIDER).replace("master_", "")
        return PlayMedia(endpoint, FORMAT_CONTENT_TYPE[HLS_PROVIDER])

    # -- browse -------------------------------------------------------------

    async def async_browse_media(self, item: MediaSourceItem) -> BrowseMediaSource:
        """Browse the camera / day / clip tree."""
        if not item.identifier:
            return self._browse_root()

        parts = item.identifier.split("|")
        kind = parts[0]
        if kind == "CAM" and len(parts) == 3:
            return self._browse_days(parts[1], int(parts[2]))
        if kind == "DAY" and len(parts) == 4:
            return await self._browse_clips(parts[1], int(parts[2]), parts[3])
        raise Unresolvable("Unknown Dahua media item '{0}'".format(item.identifier))

    def _browse_root(self) -> BrowseMediaSource:
        """The cameras that can offer recordings."""
        children: list[BrowseMediaSource] = []
        for entry in self.hass.config_entries.async_loaded_entries(DOMAIN):
            for coordinator in entry_coordinators(entry).values():
                # An indoor monitor with no camera has nothing to record.
                if coordinator.is_indoor_monitor_without_video():
                    continue
                children.append(
                    BrowseMediaSource(
                        domain=DOMAIN,
                        identifier="CAM|{0}|{1}".format(
                            entry.entry_id, coordinator.get_channel()
                        ),
                        media_class=MediaClass.DIRECTORY,
                        media_content_type=MediaType.PLAYLIST,
                        title=coordinator.get_device_name(),
                        can_play=False,
                        can_expand=True,
                    )
                )
        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=None,
            media_class=MediaClass.DIRECTORY,
            media_content_type=MediaType.PLAYLIST,
            title="Dahua",
            can_play=False,
            can_expand=True,
            children=children,
            children_media_class=MediaClass.DIRECTORY,
        )

    def _browse_days(self, entry_id: str, channel: int) -> BrowseMediaSource:
        """The last DAYS_LISTED calendar days, listed without querying."""
        today = dt_util.now().date()
        children = [
            BrowseMediaSource(
                domain=DOMAIN,
                identifier="DAY|{0}|{1}|{2}".format(entry_id, channel, day),
                media_class=MediaClass.DIRECTORY,
                media_content_type=MediaType.PLAYLIST,
                title=day,
                can_play=False,
                can_expand=True,
            )
            for day in (
                (today - timedelta(days=offset)).isoformat()
                for offset in range(DAYS_LISTED)
            )
        ]
        return BrowseMediaSource(
            domain=DOMAIN,
            identifier="CAM|{0}|{1}".format(entry_id, channel),
            media_class=MediaClass.DIRECTORY,
            media_content_type=MediaType.PLAYLIST,
            title=self._camera_name(entry_id, channel),
            can_play=False,
            can_expand=True,
            children=children,
            children_media_class=MediaClass.DIRECTORY,
        )

    async def _browse_clips(
        self, entry_id: str, channel: int, day: str
    ) -> BrowseMediaSource:
        """The clips recorded on one day for one camera."""
        records = await self._recordings(entry_id, channel, day)
        children = [
            BrowseMediaSource(
                domain=DOMAIN,
                identifier="FILE|{0}|{1}|{2}|{3}".format(
                    entry_id,
                    channel,
                    _to_underscore(str(record.get("StartTime", ""))),
                    _to_underscore(str(record.get("EndTime", ""))),
                ),
                media_class=MediaClass.VIDEO,
                media_content_type=MediaType.VIDEO,
                title=_clip_title(record),
                can_play=True,
                can_expand=False,
            )
            for record in records
            if record.get("StartTime") and record.get("EndTime")
        ]
        return BrowseMediaSource(
            domain=DOMAIN,
            identifier="DAY|{0}|{1}|{2}".format(entry_id, channel, day),
            media_class=MediaClass.DIRECTORY,
            media_content_type=MediaType.PLAYLIST,
            title="{0} {1}".format(self._camera_name(entry_id, channel), day),
            can_play=False,
            can_expand=True,
            children=children,
            children_media_class=MediaClass.VIDEO,
        )

    # -- helpers ------------------------------------------------------------

    def _coordinator(self, entry_id: str, channel: int):
        """The coordinator for one channel of one entry, or None if unloaded."""
        entry = self.hass.config_entries.async_get_entry(entry_id)
        if entry is None:
            return None
        return entry_coordinators(entry).get(channel)

    def _camera_name(self, entry_id: str, channel: int) -> str:
        coordinator = self._coordinator(entry_id, channel)
        return coordinator.get_device_name() if coordinator is not None else "Dahua"

    async def _recordings(self, entry_id: str, channel: int, day: str) -> list[dict]:
        """A day's clips, newest first, from a short-lived cache."""
        key = (entry_id, channel, day)
        cached = self._clip_cache.get(key)
        now = time.monotonic()
        if cached is not None and now - cached[0] < _CLIP_CACHE_TTL:
            return cached[1]

        coordinator = self._coordinator(entry_id, channel)
        if coordinator is None:
            raise Unresolvable("Dahua device for '{0}' is not loaded".format(entry_id))
        records = await coordinator.client.async_find_recordings(
            channel, "{0} 00:00:00".format(day), "{0} 23:59:59".format(day)
        )
        self._clip_cache[key] = (now, records)
        return records
