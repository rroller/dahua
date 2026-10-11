"""This component provides basic support for Dahua IP cameras."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import voluptuous as vol
from aiohttp import ClientError

from homeassistant.core import HomeAssistant, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_platform
from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.util import dt as dt_util

from custom_components.dahua import DahuaDataUpdateCoordinator, entry_coordinators
from custom_components.dahua import dahua_utils
from custom_components.dahua.entity import DahuaBaseEntity
from custom_components.dahua.infrared import MODE_BY_OPTION, async_write_infrared_mode
from custom_components.dahua.model_profiles import is_sdt4e425
from custom_components.dahua.rpc2 import Rpc2MethodRefused
from custom_components.dahua.vto import CancelCallRefused

from .const import (
    BACKED_UP_TABLES,
    CONF_DISABLE_BACKCHANNEL,
    DOMAIN,
)

_LOGGER: logging.Logger = logging.getLogger(__package__)

# This service handled setting the infrared mode on the camera to Off, Auto, or Manual... along with the brightness
SERVICE_SET_INFRARED_MODE = "set_infrared_mode"
SERVICE_SET_ILLUMINATOR_MODE = "set_illuminator_mode"
# This service handles setting the video profile mode to day or night
SERVICE_SET_VIDEO_PROFILE_MODE = "set_video_profile_mode"
SERVICE_SET_FOCUS_ZOOM = "set_focus_zoom"
SERVICE_SET_PRIVACY_MASKING = "set_privacy_masking"
SERVICE_SET_PRIVACY_MODE = "set_privacy_mode"
SERVICE_SET_CHANNEL_TITLE = "set_channel_title"
SERVICE_SET_TEXT_OVERLAY = "set_text_overlay"
SERVICE_SET_CUSTOM_OVERLAY = "set_custom_overlay"
SERVICE_SET_RECORD_MODE = "set_record_mode"
SERVICE_ENABLE_CHANNEL_TITLE = "enable_channel_title"
SERVICE_ENABLE_TIME_OVERLay = "enable_time_overlay"
SERVICE_ENABLE_TEXT_OVERLAY = "enable_text_overlay"
SERVICE_ENABLE_CUSTOM_OVERLAY = "enable_custom_overlay"
SERVICE_ENABLE_ALL_IVS_RULES = "enable_all_ivs_rules"
SERVICE_ENABLE_IVS_RULE = "enable_ivs_rule"
SERVICE_VTO_OPEN_DOOR = "vto_open_door"
SERVICE_VTO_CANCEL_CALL = "vto_cancel_call"
SERVICE_VTO_CALL = "vto_call"
SERVICE_SET_DAY_NIGHT_MODE = "set_video_in_day_night_mode"
SERVICE_REBOOT = "reboot"
SERVICE_GOTO_PRESET_POSITION = "goto_preset_position"
SERVICE_GET_OVERLAY_TEXT = "get_overlay_text"
SERVICE_GET_CHANNEL_TITLE = "get_channel_title"
SERVICE_GET_CONFIG = "get_config"
SERVICE_BACKUP_CONFIG = "backup_config"
SERVICE_COMPARE_CONFIG = "compare_config"

# How many changed keys a comparison will name before it stops listing them.
# A whole-device backup is a few hundred keys per table, and a comparison
# across a firmware upgrade can move most of them. The count is always
# exact; only the listing is capped, because a service response is read by a
# person.
COMPARISON_LIMIT = 200

# What a configManager config name may contain. A name goes straight into the
# getConfig URL, so this keeps it from smuggling in another CGI parameter (an
# "&action=setConfig..." tail, say); the brackets and dots are for indexed names
# like "Lighting[0][0]". Anchored so the whole name matches, not just its start,
# and used as the service schema so an invalid name is refused before any call.
_CONFIG_NAME = re.compile(r"\A[A-Za-z0-9_.\[\]]+\Z")
SERVICE_PTZ_MOVE = "ptz_move"

# What ptz.cgi calls each direction. The eight compass moves plus the two
# zoom directions, which are the same mechanism with a different code.
# What the set_infrared_mode service's `mode` means to the device. "On" is
# Manual: the service has always leaned on the client method mapping it, and
# async_write_infrared_mode compares the device's read-back against the mode it
# was given, so the translation has to happen before the call rather than inside
# it.
# `on` is the alias: the service offers On/Off/Auto and the mode select offers
# auto/manual/off, and they mean the same three device words. Built from
# MODE_BY_OPTION so there is one mapping rather than two.
INFRARED_SERVICE_MODES = {**MODE_BY_OPTION, "on": "Manual"}

PTZ_MOVE_CODES = {
    "up": "Up",
    "down": "Down",
    "left": "Left",
    "right": "Right",
    "up_left": "LeftUp",
    "up_right": "RightUp",
    "down_left": "LeftDown",
    "down_right": "RightDown",
    "zoom_in": "ZoomTele",
    "zoom_out": "ZoomWide",
}


def _read_backup(filename: str) -> dict:
    """Read a backup written by backup_config. Runs in an executor: this blocks."""
    with open(filename, encoding="utf-8") as handle:
        return json.load(handle)


def _compare_tables(was: dict, now: dict) -> dict:
    """What changed between two {table: {key: value}} maps.

    Three kinds, kept apart because they mean different things. A *changed* key
    is a setting somebody moved. A key only in the backup is one the device has
    stopped reporting, which is usually a table it no longer serves rather than
    a setting that vanished. A key only in the device is the reverse.
    """
    changed: dict = {}
    only_in_backup: dict = {}
    only_on_device: dict = {}
    for table in sorted(set(was) | set(now)):
        before = was.get(table) or {}
        after = now.get(table) or {}
        if not isinstance(before, dict) or not isinstance(after, dict):
            continue
        for key in sorted(set(before) | set(after)):
            if key not in after:
                only_in_backup.setdefault(table, []).append(key)
            elif key not in before:
                only_on_device.setdefault(table, []).append(key)
            elif str(before[key]) != str(after[key]):
                changed.setdefault(table, {})[key] = {
                    "was": before[key],
                    "now": after[key],
                }
    return {
        "changed": changed,
        "only_in_backup": only_in_backup,
        "only_on_device": only_on_device,
    }


def _count_and_cap(diff: dict, limit: int) -> dict:
    """Exact counts always; the listing capped so a person can read it."""
    changed_keys = sum(len(keys) for keys in diff["changed"].values())
    out = {
        "changed_count": changed_keys,
        "only_in_backup_count": sum(len(v) for v in diff["only_in_backup"].values()),
        "only_on_device_count": sum(len(v) for v in diff["only_on_device"].values()),
    }
    listed: dict = {}
    remaining = limit
    for table, keys in diff["changed"].items():
        if remaining <= 0:
            break
        take = dict(list(keys.items())[:remaining])
        listed[table] = take
        remaining -= len(take)
    out["changed"] = listed
    out["listing_truncated"] = changed_keys > limit
    out["only_in_backup"] = diff["only_in_backup"]
    out["only_on_device"] = diff["only_on_device"]
    return out


def _write_backup(filename: str, backup: dict) -> None:
    """Write the backup as JSON. Runs in an executor: this blocks."""
    with open(filename, "w", encoding="utf-8") as handle:
        json.dump(backup, handle, indent=2, sort_keys=True)


# One at a time, because streams and PTZ actions and these devices are measurably intolerant of
# concurrent requests: MAX_CONCURRENT_REQUESTS_PER_HOST is 2 for the same reason,
# and the login storms behind #577 and #603 are what happens without it. A
# coordinator does not help here, since it only centralises inbound reads and
# leaves outbound actions uncontrolled.
PARALLEL_UPDATES = 1


async def async_setup_entry(hass: HomeAssistant, config_entry, async_add_entities):
    """Add a Dahua IP camera from a config entry."""

    # Listed once: the capability checks further down need every channel, not
    # whichever one the loop below happened to leave behind.
    coordinators = list(entry_coordinators(config_entry).values())

    for coordinator in coordinators:
        # An indoor monitor that says it has no camera gets no camera entities.
        # getattr because the stand-in coordinators in the tests predate the
        # question.
        no_video = getattr(coordinator, "is_indoor_monitor_without_video", None)
        if no_video is not None and no_video():
            continue
        if is_sdt4e425(coordinator.get_model()):
            # This physical camera exposes two sensors. Preserve RRoller's native
            # Main/Sub/Sub_2 creation for each media channel from one config entry.
            sensors = (
                # logical channel, media channel, display name, unique-id prefix
                (0, 1, "Panorama", ""),
                (1, 2, "PTZ", "1_"),
            )
            entities = []
            for logical_channel, media_channel, sensor_name, unique_prefix in sensors:
                for stream_index in range(coordinator.get_max_streams()):
                    stream_name = coordinator.client.to_stream_name(stream_index)
                    display_name = (
                        sensor_name
                        if stream_index == 0
                        else f"{sensor_name} {stream_name}"
                    )
                    entities.append(
                        DahuaCamera(
                            coordinator,
                            stream_index,
                            config_entry,
                            logical_channel=logical_channel,
                            media_channel=media_channel,
                            display_name=display_name,
                            unique_suffix=f"{unique_prefix}{stream_name}",
                        )
                    )
            async_add_entities(entities, config_subentry_id=coordinator.subentry_id)
        else:
            max_streams = coordinator.get_max_streams()
            # Note the stream_index is 0 based. The main stream is index 0
            for stream_index in range(max_streams):
                async_add_entities(
                    [
                        DahuaCamera(
                            coordinator,
                            stream_index,
                            config_entry,
                        )
                    ],
                    config_subentry_id=coordinator.subentry_id,
                )

    # Registered once for the platform rather than once per channel:
    # entity services are platform wide, and registering the same name
    # twice raises. Note this sits outside the loop above.
    platform = entity_platform.async_get_current_platform()

    # https://developers.home-assistant.io/docs/dev_101_services/
    # "async_set_video_profile_mode" is called upon calling the service. Defined below in the DahuaCamera class
    platform.async_register_entity_service(
        SERVICE_SET_VIDEO_PROFILE_MODE,
        {
            vol.Required("mode"): vol.In(
                [
                    "Day",
                    "day",
                    "Night",
                    "night",
                ]
            )
        },
        "async_set_video_profile_mode",
    )

    platform.async_register_entity_service(
        SERVICE_SET_FOCUS_ZOOM,
        {
            vol.Required("focus", default=""): str,
            vol.Required("zoom", default=""): str,
        },
        "async_adjustfocus",
    )

    platform.async_register_entity_service(
        SERVICE_SET_PRIVACY_MASKING,
        {
            vol.Required("index", default=0): int,
            vol.Required("enabled", default=False): bool,
        },
        "async_set_privacy_masking",
    )

    platform.async_register_entity_service(
        SERVICE_SET_PRIVACY_MODE,
        {
            vol.Required("enabled", default=False): bool,
        },
        "async_set_privacy_mode",
    )

    platform.async_register_entity_service(
        SERVICE_ENABLE_CHANNEL_TITLE,
        {
            vol.Required("enabled", default=True): bool,
        },
        "async_set_enable_channel_title",
    )

    platform.async_register_entity_service(
        SERVICE_ENABLE_TIME_OVERLay,
        {
            vol.Required("enabled", default=True): bool,
        },
        "async_set_enable_time_overlay",
    )

    platform.async_register_entity_service(
        SERVICE_ENABLE_TEXT_OVERLAY,
        {
            vol.Required("group", default=1): int,
            vol.Required("enabled", default=False): bool,
        },
        "async_set_enable_text_overlay",
    )

    platform.async_register_entity_service(
        SERVICE_ENABLE_CUSTOM_OVERLAY,
        {
            vol.Required("group", default=0): int,
            vol.Required("enabled", default=False): bool,
        },
        "async_set_enable_custom_overlay",
    )

    platform.async_register_entity_service(
        SERVICE_ENABLE_ALL_IVS_RULES,
        {
            vol.Required("enabled", default=True): bool,
        },
        "async_set_enable_all_ivs_rules",
    )

    platform.async_register_entity_service(
        SERVICE_ENABLE_IVS_RULE,
        {
            vol.Required("index", default=1): int,
            vol.Required("enabled", default=True): bool,
        },
        "async_enable_ivs_rule",
    )

    platform.async_register_entity_service(
        SERVICE_VTO_OPEN_DOOR,
        {
            vol.Required("door_id", default=1): int,
        },
        "async_vto_open_door",
    )

    platform.async_register_entity_service(
        SERVICE_VTO_CANCEL_CALL, {}, "async_vto_cancel_call"
    )

    platform.async_register_entity_service(
        SERVICE_VTO_CALL,
        {
            vol.Required("room"): vol.All(str, vol.Strip, vol.Length(min=1)),
        },
        "async_vto_call",
    )

    platform.async_register_entity_service(
        SERVICE_SET_CHANNEL_TITLE,
        {
            vol.Optional("text1", default=""): str,
            vol.Optional("text2", default=""): str,
        },
        "async_set_service_set_channel_title",
    )
    platform.async_register_entity_service(
        SERVICE_SET_TEXT_OVERLAY,
        {
            vol.Required("group", default=0): int,
            vol.Optional("text1", default=""): str,
            vol.Optional("text2", default=""): str,
            vol.Optional("text3", default=""): str,
            vol.Optional("text4", default=""): str,
        },
        "async_set_service_set_text_overlay",
    )

    platform.async_register_entity_service(
        SERVICE_SET_CUSTOM_OVERLAY,
        {
            vol.Required("group", default=0): int,
            vol.Optional("text1", default=""): str,
            vol.Optional("text2", default=""): str,
        },
        "async_set_service_set_custom_overlay",
    )

    platform.async_register_entity_service(
        SERVICE_SET_DAY_NIGHT_MODE,
        {
            vol.Required("config_type"): vol.In(
                ["general", "General", "day", "Day", "night", "Night", "0", "1", "2"]
            ),
            vol.Required("mode"): vol.In(
                [
                    "color",
                    "Color",
                    "brightness",
                    "Brightness",
                    "blackwhite",
                    "BlackWhite",
                    "Auto",
                    "auto",
                ]
            ),
        },
        "async_set_video_in_day_night_mode",
    )

    platform.async_register_entity_service(SERVICE_REBOOT, {}, "async_reboot")

    platform.async_register_entity_service(
        SERVICE_SET_RECORD_MODE,
        {
            vol.Required("mode"): vol.In(
                [
                    "On",
                    "on",
                    "Off",
                    "off",
                    "Auto",
                    "auto",
                    "0",
                    "1",
                    "2",
                ]
            )
        },
        "async_set_record_mode",
    )

    # Exposes a service to enable setting the cameras infrared light to Auto, Manual, and Off along with the brightness
    #
    # `any`, not the loop variable. Entity services are registered once for the
    # whole platform, so a recorder asking the last channel it happened to iterate
    # decided this for every channel: a recorder whose channel 0 has an
    # illuminator and whose last channel does not lost the service for all of
    # them. And an entry with no channels -- what entry_coordinators returns for a
    # setup that failed or one being torn down -- left `coordinator` unbound and
    # raised UnboundLocalError here, taking the platform's other services with it.
    if any(c.supports_infrared_light() for c in coordinators):
        # "async_set_infrared_mode" is the method called upon calling the service. Defined below in DahuaCamera class
        platform.async_register_entity_service(
            SERVICE_SET_INFRARED_MODE,
            {
                vol.Required("mode"): vol.In(
                    ["On", "on", "Off", "off", "Auto", "auto"]
                ),
                vol.Optional("brightness", default=100): vol.All(
                    vol.Coerce(int), vol.Range(min=0, max=100)
                ),
            },
            "async_set_infrared_mode",
        )

    # The light entity can only say on or off. Off is not the same as automatic,
    # and without this there is no way back to the camera's own behaviour.
    if any(c.supports_illuminator() for c in coordinators):
        platform.async_register_entity_service(
            SERVICE_SET_ILLUMINATOR_MODE,
            {
                vol.Required("mode"): vol.In(
                    ["On", "on", "Off", "off", "Auto", "auto"]
                ),
                vol.Optional("brightness", default=100): vol.All(
                    vol.Coerce(int), vol.Range(min=0, max=100)
                ),
            },
            "async_set_illuminator_mode",
        )

    platform.async_register_entity_service(
        SERVICE_PTZ_MOVE,
        {
            vol.Required("direction"): vol.In(sorted(PTZ_MOVE_CODES)),
            vol.Optional("speed", default=4): vol.All(
                vol.Coerce(int), vol.Range(min=1, max=8)
            ),
            vol.Optional("duration", default=0.5): vol.All(
                vol.Coerce(float), vol.Range(min=0.1, max=10)
            ),
        },
        "async_ptz_move",
    )

    platform.async_register_entity_service(
        SERVICE_GET_OVERLAY_TEXT,
        {
            vol.Optional("group", default=0): vol.All(
                vol.Coerce(int), vol.Range(min=0, max=100)
            ),
        },
        "async_get_overlay_text",
        supports_response=SupportsResponse.ONLY,
    )

    platform.async_register_entity_service(
        SERVICE_GET_CHANNEL_TITLE,
        {},
        "async_get_channel_title",
        supports_response=SupportsResponse.ONLY,
    )

    platform.async_register_entity_service(
        SERVICE_GET_CONFIG,
        {vol.Required("name"): vol.All(str, vol.Match(_CONFIG_NAME))},
        "async_get_config_service",
        supports_response=SupportsResponse.ONLY,
    )

    platform.async_register_entity_service(
        SERVICE_COMPARE_CONFIG,
        {vol.Required("filename"): vol.All(str, vol.Length(min=1))},
        "async_compare_config",
        supports_response=SupportsResponse.ONLY,
    )

    platform.async_register_entity_service(
        SERVICE_BACKUP_CONFIG,
        {vol.Optional("filename"): vol.All(str, vol.Length(min=1))},
        "async_backup_config",
        # OPTIONAL, not ONLY: the backup is useful on screen in Developer
        # Tools and useful as a file, and which one somebody wants depends on
        # whether they are looking or keeping.
        supports_response=SupportsResponse.OPTIONAL,
    )

    platform.async_register_entity_service(
        SERVICE_GOTO_PRESET_POSITION,
        {
            vol.Required("position", default=1): vol.All(
                vol.Coerce(int), vol.Range(min=1, max=10)
            ),
        },
        "async_goto_preset_position",
    )


def rtsp_stream_source(url: str, disable_backchannel: bool) -> str:
    """The RTSP URL handed to Home Assistant's stream consumers.

    go2rtc opens the RTSP backchannel (two-way audio) by default and holds it
    for as long as it streams. Doorbells only have one, so while HA watches,
    the doorbell shows a call in progress and the vendor app cannot talk.
    go2rtc reads the #backchannel=0 fragment and leaves the channel alone.
    """
    if disable_backchannel:
        return url + "#backchannel=0"
    return url


class DahuaCamera(DahuaBaseEntity, Camera):
    """An implementation of a Dahua IP camera."""

    def __init__(
        self,
        coordinator: DahuaDataUpdateCoordinator,
        stream_index: int,
        config_entry,
        *,
        logical_channel: int | None = None,
        media_channel: int | None = None,
        display_name: str | None = None,
        unique_suffix: str | None = None,
    ):
        """Initialize the Dahua camera."""
        DahuaBaseEntity.__init__(self, coordinator, config_entry)
        Camera.__init__(self)
        stream_name = coordinator.client.to_stream_name(stream_index)
        self._logical_channel = (
            coordinator.get_channel() if logical_channel is None else logical_channel
        )
        self._channel_number = (
            coordinator.get_channel_number() if media_channel is None else media_channel
        )
        self._coordinator = coordinator
        self._name = display_name if display_name else stream_name
        suffix = unique_suffix or stream_name
        self._unique_id = coordinator.get_serial_number() + "_" + suffix
        self._stream_index = stream_index
        self._motion_status = False
        self._stream_source = rtsp_stream_source(
            coordinator.client.get_rtsp_stream_url(self._channel_number, stream_index),
            coordinator.channel_option(CONF_DISABLE_BACKCHANNEL, False),
        )

    @property
    def unique_id(self):
        """Return the entity unique ID."""
        return self._unique_id

    @property
    def entity_registry_enabled_default(self) -> bool:
        """Only the main stream is enabled to begin with.

        Every stream the device can serve gets an entity, whether or not it is
        enabled on the device, because which ones exist cannot be known without
        asking and asking costs a request per channel. Most people watch one stream
        per camera, so on an eleven channel recorder serving three streams each that
        was thirty three camera entities to go and delete by hand -- which the README
        said to do, in as many words.

        Created but not enabled is the difference: the entity is still listed, and
        anyone pointing a card at a sub stream turns it on once. Derived from the
        stream index rather than stored in `_attr_entity_registry_enabled_default`,
        so there is one place it can be wrong and it can be read off an instance
        without the entity machinery.

        This is consulted only when an entity is first registered, so nothing that
        already exists changes: an existing sub stream camera stays exactly as its
        owner left it.
        """
        return self._stream_index == 0

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ):
        """Return a still image response from the camera, or None if it refused.

        These devices refuse a snapshot under load, and a recorder refuses more
        often because every channel is competing for the same box. Letting that
        out of here turns an ordinary transient refusal into a failed
        `camera.snapshot` service call, which is what #290 is: an automation
        that saves a picture stops working for reasons that have nothing to do
        with the automation.

        Returning None is what Home Assistant expects from a camera that cannot
        produce an image right now, and it leaves the previous one in place
        rather than replacing it with an error.

        Only transport failures are caught. Anything else still comes out,
        because a bug in here should not be quietly turned into a blank frame.
        """
        try:
            return await self._coordinator.client.async_get_snapshot(
                self._channel_number
            )
        except (ClientError, TimeoutError, asyncio.TimeoutError) as error:
            _LOGGER.debug("%s: could not fetch a snapshot: %s", self._name, error)
            return None

    @property
    def supported_features(self):
        """Flag supported features."""
        return CameraEntityFeature.STREAM

    async def stream_source(self):
        """Return the RTSP stream source."""
        return self._stream_source

    @property
    def motion_detection_enabled(self):
        """Camera Motion Detection Status."""
        return self._coordinator.is_motion_detection_enabled()

    async def async_enable_motion_detection(self):
        """Enable motion detection in camera."""
        try:
            channel = self._logical_channel
            await self._coordinator.client.enable_motion_detection(channel, True)
            await self._coordinator.async_refresh()
        except TypeError:
            _LOGGER.debug(
                "Failed enabling motion detection on '%s'. Is it supported by the device?",
                self._name,
            )

    async def async_disable_motion_detection(self):
        """Disable motion detection."""
        try:
            channel = self._logical_channel
            await self._coordinator.client.enable_motion_detection(channel, False)
            await self._coordinator.async_refresh()
        except TypeError:
            _LOGGER.debug(
                "Failed disabling motion detection on '%s'. Is it supported by the device?",
                self._name,
            )

    @property
    def name(self):
        """Return the name of this camera."""
        return self._name

    async def async_set_infrared_mode(self, mode: str, brightness: int):
        """Handles the service call from SERVICE_SET_INFRARED_MODE to set infrared mode and brightness

        Through the same path the light entity and the mode select use. This
        called the v1 client method directly, so it got none of what that path
        has learnt: the fallback to Lighting_V2 on a channel whose `Lighting`
        writes the device refuses outright (measured on a DHI-NVR5464-16P-EI,
        403 `Authority:check failure.` on every channel over both transports),
        the refusal store that stops asking after one of those, and the
        read-back check that turns a write the device accepted and ignored into
        an error the caller sees. So on those channels the light entity worked
        and this service failed silently.
        """
        if self._logical_channel != self._coordinator.get_channel():
            # The SDT4E425's second sensor is a camera entity on another channel
            # of one coordinator. async_write_infrared_mode reads the profile,
            # the brightness bank, the Lighting_V2 row and the refusal store off
            # the coordinator's own channel, so it cannot address this one, and
            # its read-back would compare the wrong row. Keep the direct write,
            # which is what this entity has always done.
            await self._coordinator.client.async_set_lighting_v1_mode(
                self._logical_channel,
                mode,
                brightness,
                self._coordinator.get_infrared_profile(),
                self._coordinator.get_infrared_bank(),
            )
            await self._coordinator.async_refresh()
            return

        # async_write_infrared_mode refreshes the coordinator itself.
        await async_write_infrared_mode(
            self._coordinator, INFRARED_SERVICE_MODES[mode.lower()], brightness
        )

    async def async_set_illuminator_mode(self, mode: str, brightness: int):
        """Handles SERVICE_SET_ILLUMINATOR_MODE: illuminator mode and brightness.

        Uses the same resolved light index and brightness bank as the light
        entity, so the service and the toggle address the same physical light.
        """
        channel = self._logical_channel
        await self._coordinator.client.async_set_lighting_v2_mode(
            channel,
            mode,
            brightness,
            self._coordinator.get_profile_mode(),
            self._coordinator.get_illuminator_index(),
            self._coordinator.get_illuminator_bank(),
        )
        await self._coordinator.async_refresh()

    async def async_ptz_move(self, direction: str, speed: int, duration: float):
        """Move the camera in a direction for a moment.

        #534 and #720 both asked for this. Everything here drove ptz.cgi
        already, but only ever with GotoPreset, so a camera that can pan
        and tilt could only be sent to positions somebody had saved on it
        first.
        """
        code = PTZ_MOVE_CODES[direction]
        await self._coordinator.client.async_ptz_move(
            self._channel_number, code, speed, duration
        )
        await self._coordinator.async_refresh()

    async def async_goto_preset_position(self, position: int):
        """Go to a preset, using RPC2 only for the SDT4E425."""
        channel = self._channel_number
        if is_sdt4e425(self._coordinator.get_model()):
            await self._coordinator.client.async_goto_preset_rpc2(1, position)
        else:
            await self._coordinator.client.async_goto_preset_position(channel, position)
        await self._coordinator.async_refresh()

    async def async_set_video_in_day_night_mode(self, config_type: str, mode: str):
        """Handles the service call from SERVICE_SET_DAY_NIGHT_MODE to set the day/night color mode"""
        channel = self._logical_channel
        await self._coordinator.client.async_set_video_in_day_night_mode(
            channel, config_type, mode
        )
        await self._coordinator.async_refresh()

    async def async_reboot(self):
        """Handles the service call from SERVICE_REBOOT to reboot the device"""
        await self._coordinator.client.reboot()

    async def async_set_record_mode(self, mode: str):
        """Handles the service call from SERVICE_SET_RECORD_MODE to set the record mode"""
        channel = self._logical_channel
        await self._coordinator.client.async_set_record_mode(channel, mode)
        await self._coordinator.async_refresh()

    async def async_set_video_profile_mode(self, mode: str):
        """Handles the service call from SERVICE_SET_VIDEO_PROFILE_MODE to set profile mode to day/night"""
        channel = self._logical_channel
        model = self._coordinator.get_model()
        # Some NVRs like the Lorex DHI-NVR4108HS-8P-4KS2 change the day/night mode through a switch
        if any(substring in model for substring in ["NVR4108HS", "IPC-Color4K"]):
            await self._coordinator.client.async_set_night_switch_mode(channel, mode)
        else:
            # VideoInMode comes in three shapes, and this writes Config[0], which only
            # selects the profile in the ordinary one. On the IL (ConfigEx) and general
            # (Config[0]=2) shapes the write does not switch the profile: the #458
            # reporter's camera threw on it, and a recorder measured here accepted it
            # with 200 and rendered from the profile it was already on. Either way the
            # service cannot do what it says, so it refuses with a reason rather than
            # firing a write that errors or is silently ignored. The shape is read per
            # channel from the last poll, because one recorder carries all three at once.
            if not self._coordinator.video_profile_mode_is_writable():
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="video_profile_not_switchable",
                    translation_placeholders={
                        "device": self._coordinator.get_device_name(),
                        "shape": self._coordinator.describe_video_profile_shape(),
                    },
                )
            await self._coordinator.client.async_set_video_profile_mode(channel, mode)
        # The profile decides which Lighting row every light command writes to,
        # and the poll is what reads it back. Without this the next light
        # command in the same poll window is written to the row the camera is
        # not rendering from, where the device accepts and ignores it.
        await self._coordinator.async_refresh()

    async def async_adjustfocus(self, focus: str, zoom: str):
        """Handles the service call from SERVICE_SET_INFRARED_MODE to set zoom and focus"""
        await self._coordinator.client.async_adjustfocus_v1(focus, zoom)
        await self._coordinator.async_refresh()

    async def async_set_privacy_masking(self, index: int, enabled: bool):
        """Handles the service call from SERVICE_SET_PRIVACY_MASKING to control the privacy masking"""
        await self._coordinator.client.async_setprivacymask(index, enabled)

    async def async_set_privacy_mode(self, enabled: bool):
        """Handles the service call from SERVICE_SET_PRIVACY_MODE to control the lens privacy mask"""
        await self._coordinator.client.async_set_privacy_mode(enabled)
        await self._coordinator.async_refresh()

    async def async_set_enable_channel_title(self, enabled: bool):
        """Handles the service call from SERVICE_ENABLE_CHANNEL_TITLE"""
        channel = self._logical_channel
        await self._coordinator.client.async_enable_channel_title(channel, enabled)

    async def async_set_enable_time_overlay(self, enabled: bool):
        """Handles the service call from SERVICE_ENABLE_TIME_OVERLAY"""
        channel = self._logical_channel
        await self._coordinator.client.async_enable_time_overlay(channel, enabled)

    async def async_set_enable_text_overlay(self, group: int, enabled: bool):
        """Handles the service call from SERVICE_ENABLE_TEXT_OVERLAY"""
        channel = self._logical_channel
        await self._coordinator.client.async_enable_text_overlay(
            channel, group, enabled
        )

    async def async_set_enable_custom_overlay(self, group: int, enabled: bool):
        """Handles the service call from SERVICE_ENABLE_CUSTOM_OVERLAY"""
        channel = self._logical_channel
        await self._coordinator.client.async_enable_custom_overlay(
            channel, group, enabled
        )

    async def async_set_enable_all_ivs_rules(self, enabled: bool):
        """Handles the service call from SERVICE_ENABLE_ALL_IVS_RULES"""
        channel = self._logical_channel
        await self._coordinator.client.async_set_all_ivs_rules(channel, enabled)

    async def async_enable_ivs_rule(self, index: int, enabled: bool):
        """Handles the service call from SERVICE_ENABLE_IVS_RULE"""
        channel = self._logical_channel
        await self._coordinator.client.async_set_ivs_rule(channel, index, enabled)

    async def async_vto_open_door(self, door_id: int):
        """Handles the service call from SERVICE_VTO_OPEN_DOOR"""
        # The service is offered on every camera entity, and the Open Door
        # button is only created on a doorbell; aimed at anything else the CGI
        # endpoint is not there and the user gets a raw HTTP error. The sibling
        # cancel-call service says which device it is for, so this one does too.
        # getattr because tests build stand-in coordinators without the method.
        is_doorbell = getattr(self._coordinator, "is_doorbell", None)
        if is_doorbell is not None and not is_doorbell():
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="open_door_needs_a_doorbell",
                translation_placeholders={
                    "device": self._coordinator.get_device_name()
                },
            )
        await self._coordinator.client.async_access_control_open_door(door_id)

    async def async_vto_cancel_call(self):
        """Handles the service call from SERVICE_VTO_CANCEL_CALL to cancel VTO calls"""
        # The service is offered on every camera entity, and only a doorbell
        # ever has a VTO client: on anything else this is None, and so was the
        # error -- AttributeError on NoneType, with a traceback and no clue that
        # the wrong entity had been picked. A doorbell between reconnects lands
        # here too.
        vto_client = self._coordinator.get_vto_client()
        if vto_client is None:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="no_vto_connection_for_service",
                translation_placeholders={
                    "device": self._coordinator.get_device_name()
                },
            )
        try:
            await vto_client.cancel_call()
        except CancelCallRefused as refused:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="cancel_call_refused",
                translation_placeholders={"reason": str(refused)},
            ) from refused

    async def async_vto_call(self, room: str):
        """Handles the service call from SERVICE_VTO_CALL to ring a room from a VTO"""
        # Offered on every camera entity, like open door, and for the same reason
        # it says so when aimed at something that is not a doorbell rather than
        # leaving the user an RPC2 refusal from a camera that has no VideoTalkPhone.
        is_doorbell = getattr(self._coordinator, "is_doorbell", None)
        if is_doorbell is not None and not is_doorbell():
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="vto_call_needs_a_doorbell",
                translation_placeholders={
                    "device": self._coordinator.get_device_name()
                },
            )
        try:
            await self._coordinator.client.async_vto_call(room)
        except Rpc2MethodRefused as refused:
            # The device's own reason, code and message, is the useful part: a
            # room it does not know and a login it will not take read the same
            # from here otherwise.
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="vto_call_refused",
                translation_placeholders={"room": room, "reason": str(refused)},
            ) from refused

    async def async_set_service_set_channel_title(self, text1: str, text2: str):
        """Handles the service call from SERVICE_SET_CHANNEL_TITLE to set profile mode to day/night"""
        channel = self._logical_channel
        await self._coordinator.client.async_set_service_set_channel_title(
            channel, text1, text2
        )

    async def async_get_overlay_text(self, group: int) -> dict:
        """Read back the overlay text this camera is showing.

        #461. Five services write overlays and none of them read one, so
        the text could be set from here and never asked about again, and
        it can be changed on the camera itself as well.

        The two names mirror the services that write them, which is the
        only thing that makes them guessable: set_text_overlay writes
        CustomTitle and set_custom_overlay writes UserDefinedTitle.
        Reading them back under the writer's name rather than the table's
        keeps the pair usable together.
        """
        channel = self._logical_channel
        data = await self._coordinator.client.async_get_video_widget()
        return {
            "text_overlay": dahua_utils.parse_overlay_lines(
                data.get(
                    "table.VideoWidget[{0}].CustomTitle[{1}].Text".format(
                        channel, group
                    )
                )
            ),
            "custom_overlay": dahua_utils.parse_overlay_lines(
                data.get(
                    "table.VideoWidget[{0}].UserDefinedTitle[{1}].Text".format(
                        channel, group
                    )
                )
            ),
        }

    async def async_get_channel_title(self) -> dict:
        """Read back this channel's title overlay.

        #980, the companion to set_channel_title the way get_overlay_text is to
        the overlay setters. A separate service rather than another key on
        get_overlay_text so #461's response shape does not change, and because
        the title has no overlay group to pass. Reads the same ChannelTitle
        table the add flow already reads, and returns "" rather than null for a
        channel the device does not list, so a template can use it directly.
        """
        titles = dahua_utils.parse_channel_titles(
            await self._coordinator.client.async_get_config("ChannelTitle")
        )
        return {"channel_title": titles.get(self._logical_channel, "")}

    async def async_get_config_service(self, name: str) -> dict:
        """Read any configuration table from the device, for diagnostics.

        A deliberate read-only escape hatch: this integration wraps the common
        config tables, but a device has many model-specific ones it does not,
        and inspecting them meant enabling debug logging. getConfig cannot change
        the device, so unlike an arbitrary-CGI passthrough this cannot be used to
        write, which matters here -- several config writes on these devices are
        irreversible or accepted-but-ignored.

        The name is validated by the service schema (_CONFIG_NAME) before this
        runs, because it goes straight into the getConfig URL and an unchecked
        name could append another CGI parameter.
        """
        return {"config": await self._coordinator.client.async_get_config(name)}

    async def async_compare_config(self, filename: str) -> dict:
        """What has changed on this device since a backup was taken.

        A backup nobody can compare against is a file, not a tool. The question
        people actually have is "what moved since I took this", and answering it
        by hand means diffing a few hundred keys across up to nineteen tables.

        It matters more here than in most places because several Dahua config
        writes cannot be undone from Home Assistant -- VideoAnalyseRule enables
        over CGI and will not disable, and some writes are accepted and then
        ignored. The discipline that works on this hardware is to record the
        settings, make one change, and look at what actually moved. This is the
        looking.

        Read-only at both ends: it re-reads the same tables `backup_config`
        reads, and the file is opened through the same allowlist.
        """
        if not self.hass.config.is_allowed_path(filename):
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="backup_path_not_allowed",
                translation_placeholders={"filename": filename},
            )
        try:
            saved = await self.hass.async_add_executor_job(_read_backup, filename)
        except (OSError, ValueError) as error:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="backup_could_not_be_read",
                translation_placeholders={
                    "filename": filename,
                    "reason": str(error),
                },
            ) from error

        if not isinstance(saved, dict) or not isinstance(saved.get("tables"), dict):
            # Named rather than treated as an empty backup, which would report
            # every setting as newly appeared and read as a catastrophe.
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="backup_is_not_a_backup",
                translation_placeholders={"filename": filename},
            )

        current = await self.async_backup_config()
        diff = _compare_tables(saved["tables"], current["tables"])
        answer = _count_and_cap(diff, COMPARISON_LIMIT)
        answer["backup_taken"] = saved.get("created")
        answer["compared_at"] = current["created"]
        answer["backup_device"] = saved.get("device")
        answer["device"] = current["device"]
        # A table the device has stopped serving is reported here rather than as
        # several hundred vanished keys, because that is what it means.
        answer["refused_now"] = current["refused"]
        answer["refused_when_backed_up"] = saved.get("refused", [])
        return answer

    async def async_backup_config(self, filename: str | None = None) -> dict:
        """Read back every setting this integration can change.

        Asked for because a Dahua config write is frequently not reversible
        from here: VideoAnalyseRule enables over CGI and will not disable, and
        several writes are accepted and then ignored. Once something has
        changed there is often no way to find out what it used to be, and the
        device's own interface does not keep history either.

        So this is a backup in the useful sense -- a record of what the
        settings were -- rather than anything that can restore them by itself.
        Restoring means setting them back one at a time, and this says what to
        set them to. Claiming more than that would be a lie: the write side
        cannot put several of these back.

        Scope is BACKED_UP_TABLES, which is every table with a write path in
        this integration, kept honest by a test rather than by hand. A table
        the device does not serve comes back empty -- async_get_config returns
        {} for a refused read -- so it is reported under `refused` rather than
        silently missing, because "this model has no such table" and "the
        backup skipped it" look identical otherwise.

        With a filename it also writes the backup as JSON, through the same
        allowlist Home Assistant puts on every other path an integration
        writes to. Without one it just returns, which is what Developer Tools
        shows.

        **It covers the whole device, not one channel.** `getConfig` reads a
        whole table and most of these have no per-channel form, so running this
        against one channel of a recorder returns every channel's values --
        measured as 390 VideoColor keys across twelve channels on a
        DHI-NVR5464-16P-EI. That makes one call per device enough, and it makes
        the channel the call was made from almost irrelevant, which is why the
        answer says so rather than labelling itself with that channel.
        """
        tables: dict = {}
        refused: list = []
        for name in BACKED_UP_TABLES:
            answer = await self._coordinator.client.async_get_config(name)
            if answer:
                tables[name] = answer
            else:
                refused.append(name)

        backup = {
            "created": dt_util.utcnow().isoformat(),
            "device": {
                "name": self._coordinator.get_device_name(),
                "model": self._coordinator.get_model(),
                "firmware": self._coordinator.get_firmware_version(),
                # The channel this was *run from*, which is not the channel the
                # backup is of -- see `covers` below. Named as such because the
                # first version called it "channel" beside a table holding
                # every channel on the recorder, and that reads as a per
                # channel backup to anybody restoring from it.
                "read_from_channel": self._coordinator.get_channel(),
            },
            # Measured on a DHI-NVR5464-16P-EI the day this shipped: running
            # this against one channel returned VideoColor carrying all twelve
            # of the recorder's channels, 390 keys. getConfig reads a whole
            # table; there is no per-channel form of most of these. So the
            # backup is of the device, and saying so is the difference between
            # a useful record and a misleading one.
            "covers": (
                "every channel of this device. These are whole-table reads, so "
                "a table here holds the values for all channels, not only the "
                "one this was run from."
            ),
            "tables": tables,
            # Named, not omitted. See the docstring.
            "refused": refused,
        }

        if filename:
            # The same check camera snapshots use. Without it a service call
            # could write anywhere the Home Assistant process can reach.
            if not self.hass.config.is_allowed_path(filename):
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="backup_path_not_allowed",
                    translation_placeholders={"filename": filename},
                )
            try:
                await self.hass.async_add_executor_job(_write_backup, filename, backup)
            except OSError as error:
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="backup_could_not_be_written",
                    translation_placeholders={
                        "filename": filename,
                        "reason": str(error),
                    },
                ) from error
            backup["written_to"] = filename

        return backup

    async def async_set_service_set_text_overlay(
        self, group: int, text1: str, text2: str, text3: str, text4: str
    ):
        """Handles the service call from SERVICE_SET_TEXT_OVERLAY to set profile mode to day/night"""
        channel = self._logical_channel
        await self._coordinator.client.async_set_service_set_text_overlay(
            channel, group, text1, text2, text3, text4
        )

    async def async_set_service_set_custom_overlay(
        self, group: int, text1: str, text2: str
    ):
        """Handles the service call from SERVICE_SET_CUSTOM_OVERLAY to set profile mode to day/night"""
        channel = self._logical_channel
        await self._coordinator.client.async_set_service_set_custom_overlay(
            channel, group, text1, text2
        )
