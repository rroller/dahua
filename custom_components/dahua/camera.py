"""This component provides basic support for Dahua IP cameras."""
from __future__ import annotations

import asyncio
import logging
import voluptuous as vol
from aiohttp import ClientError

from homeassistant.core import HomeAssistant, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_platform
from homeassistant.components.camera import Camera, CameraEntityFeature

from custom_components.dahua import DahuaDataUpdateCoordinator
from custom_components.dahua.entity import DahuaBaseEntity
from custom_components.dahua.model_profiles import is_sdt4e425
from custom_components.dahua.vto import CancelCallRefused

from .const import (
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
SERVICE_SET_DAY_NIGHT_MODE = "set_video_in_day_night_mode"
SERVICE_REBOOT = "reboot"
SERVICE_GOTO_PRESET_POSITION = "goto_preset_position"
SERVICE_GET_OVERLAY_TEXT = "get_overlay_text"
SERVICE_PTZ_MOVE = "ptz_move"

# What ptz.cgi calls each direction. The eight compass moves plus the two
# zoom directions, which are the same mechanism with a different code.
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


async def async_setup_entry(hass: HomeAssistant, config_entry, async_add_entities):
    """Add a Dahua IP camera from a config entry."""

    coordinator: DahuaDataUpdateCoordinator = hass.data[DOMAIN][config_entry.entry_id]
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
        async_add_entities(entities)
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
                ]
            )

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
                ])
        },
        "async_set_video_profile_mode"
    )

    platform.async_register_entity_service(
        SERVICE_SET_FOCUS_ZOOM,
        {
            vol.Required("focus", default=""): str,
            vol.Required("zoom", default=""): str,
        },
        "async_adjustfocus"
    )

    platform.async_register_entity_service(
        SERVICE_SET_PRIVACY_MASKING,
        {
            vol.Required("index", default=0): int,
            vol.Required("enabled", default=False): bool,
        },
        "async_set_privacy_masking"
    )

    platform.async_register_entity_service(
        SERVICE_SET_PRIVACY_MODE,
        {
            vol.Required("enabled", default=False): bool,
        },
        "async_set_privacy_mode"
    )

    platform.async_register_entity_service(
        SERVICE_ENABLE_CHANNEL_TITLE,
        {
            vol.Required("enabled", default=True): bool,
        },
        "async_set_enable_channel_title"
    )

    platform.async_register_entity_service(
        SERVICE_ENABLE_TIME_OVERLay,
        {
            vol.Required("enabled", default=True): bool,
        },
        "async_set_enable_time_overlay"
    )

    platform.async_register_entity_service(
        SERVICE_ENABLE_TEXT_OVERLAY,
        {
            vol.Required("group", default=1): int,
            vol.Required("enabled", default=False): bool,
        },
        "async_set_enable_text_overlay"
    )

    platform.async_register_entity_service(
        SERVICE_ENABLE_CUSTOM_OVERLAY,
        {
            vol.Required("group", default=0): int,
            vol.Required("enabled", default=False): bool,
        },
        "async_set_enable_custom_overlay"
    )

    platform.async_register_entity_service(
        SERVICE_ENABLE_ALL_IVS_RULES,
        {
            vol.Required("enabled", default=True): bool,
        },
        "async_set_enable_all_ivs_rules"
    )

    platform.async_register_entity_service(
        SERVICE_ENABLE_IVS_RULE,
        {
            vol.Required("index", default=1): int,
            vol.Required("enabled", default=True): bool,
        },
        "async_enable_ivs_rule"
    )

    platform.async_register_entity_service(
        SERVICE_VTO_OPEN_DOOR,
        {
            vol.Required("door_id", default=1): int,
        },
        "async_vto_open_door"
    )

    platform.async_register_entity_service(
        SERVICE_VTO_CANCEL_CALL,
        {},
        "async_vto_cancel_call"
    )

    platform.async_register_entity_service(
        SERVICE_SET_CHANNEL_TITLE,
        {
            vol.Optional("text1", default=""): str,
            vol.Optional("text2", default=""): str,
        },
        "async_set_service_set_channel_title"
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
        "async_set_service_set_text_overlay"
    )

    platform.async_register_entity_service(
        SERVICE_SET_CUSTOM_OVERLAY,
        {
            vol.Required("group", default=0): int,
            vol.Optional("text1", default=""): str,
            vol.Optional("text2", default=""): str,
        },
        "async_set_service_set_custom_overlay"
    )

    platform.async_register_entity_service(
        SERVICE_SET_DAY_NIGHT_MODE,
        {
            vol.Required("config_type"): vol.In(["general", "General", "day", "Day", "night", "Night", "0", "1", "2"]),
            vol.Required("mode"): vol.In(["color", "Color", "brightness", "Brightness", "blackwhite", "BlackWhite",
                                          "Auto", "auto"])
        },
        "async_set_video_in_day_night_mode"
    )

    platform.async_register_entity_service(
        SERVICE_REBOOT,
        {},
        "async_reboot"
    )

    platform.async_register_entity_service(
        SERVICE_SET_RECORD_MODE,
        {
            vol.Required("mode"): vol.In(["On", "on", "Off", "off", "Auto", "auto", "0", "1", "2", ])
        },
        "async_set_record_mode"
    )

    # Exposes a service to enable setting the cameras infrared light to Auto, Manual, and Off along with the brightness
    if coordinator.supports_infrared_light():
        # "async_set_infrared_mode" is the method called upon calling the service. Defined below in DahuaCamera class
        platform.async_register_entity_service(
            SERVICE_SET_INFRARED_MODE,
            {
                vol.Required("mode"): vol.In(["On", "on", "Off", "off", "Auto", "auto"]),
                vol.Optional('brightness', default=100): vol.All(vol.Coerce(int), vol.Range(min=0, max=100)),
            },
            "async_set_infrared_mode"
        )

    # The light entity can only say on or off. Off is not the same as automatic,
    # and without this there is no way back to the camera's own behaviour.
    if coordinator.supports_illuminator():
        platform.async_register_entity_service(
            SERVICE_SET_ILLUMINATOR_MODE,
            {
                vol.Required("mode"): vol.In(["On", "on", "Off", "off", "Auto", "auto"]),
                vol.Optional('brightness', default=100): vol.All(vol.Coerce(int), vol.Range(min=0, max=100)),
            },
            "async_set_illuminator_mode"
        )

    platform.async_register_entity_service(
        SERVICE_PTZ_MOVE,
        {
            vol.Required('direction'): vol.In(sorted(PTZ_MOVE_CODES)),
            vol.Optional('speed', default=4):
                vol.All(vol.Coerce(int), vol.Range(min=1, max=8)),
            vol.Optional('duration', default=0.5):
                vol.All(vol.Coerce(float), vol.Range(min=0.1, max=10)),
        },
        "async_ptz_move"
    )

    platform.async_register_entity_service(
        SERVICE_GET_OVERLAY_TEXT,
        {
            vol.Optional('group', default=0):
                vol.All(vol.Coerce(int), vol.Range(min=0, max=100)),
        },
        "async_get_overlay_text",
        supports_response=SupportsResponse.ONLY,
    )

    platform.async_register_entity_service(
        SERVICE_GOTO_PRESET_POSITION,
        {
            vol.Required('position', default=1): vol.All(vol.Coerce(int), vol.Range(min=1, max=10)),
        },
        "async_goto_preset_position"
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
        self, coordinator: DahuaDataUpdateCoordinator, stream_index: int, config_entry,
        *, logical_channel: int | None = None, media_channel: int | None = None,
        display_name: str | None = None, unique_suffix: str | None = None,
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
        self._name = (
            f"{config_entry.title} {display_name}"
            if display_name else f"{config_entry.title} {stream_name}"
        )
        suffix = unique_suffix or stream_name
        self._unique_id = coordinator.get_serial_number() + "_" + suffix
        self._stream_index = stream_index
        self._motion_status = False
        self._stream_source = rtsp_stream_source(
            coordinator.client.get_rtsp_stream_url(self._channel_number, stream_index),
            config_entry.options.get(CONF_DISABLE_BACKCHANNEL, False),
        )

    @property
    def unique_id(self):
        """Return the entity unique ID."""
        return self._unique_id

    async def async_camera_image(self, width: int | None = None, height: int | None = None):
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
            return await self._coordinator.client.async_get_snapshot(self._channel_number)
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
            _LOGGER.debug("Failed enabling motion detection on '%s'. Is it supported by the device?", self._name)

    async def async_disable_motion_detection(self):
        """Disable motion detection."""
        try:
            channel = self._logical_channel
            await self._coordinator.client.enable_motion_detection(channel, False)
            await self._coordinator.async_refresh()
        except TypeError:
            _LOGGER.debug("Failed disabling motion detection on '%s'. Is it supported by the device?", self._name)

    @property
    def name(self):
        """Return the name of this camera."""
        return self._name

    async def async_set_infrared_mode(self, mode: str, brightness: int):
        """ Handles the service call from SERVICE_SET_INFRARED_MODE to set infrared mode and brightness """
        channel = self._logical_channel
        await self._coordinator.client.async_set_lighting_v1_mode(
            channel, mode, brightness, self._coordinator.get_infrared_profile())
        await self._coordinator.async_refresh()

    async def async_set_illuminator_mode(self, mode: str, brightness: int):
        """Handles SERVICE_SET_ILLUMINATOR_MODE: illuminator mode and brightness.

        Uses the same resolved light index and brightness bank as the light
        entity, so the service and the toggle address the same physical light.
        """
        channel = self._logical_channel
        await self._coordinator.client.async_set_lighting_v2_mode(
            channel, mode, brightness, self._coordinator.get_profile_mode(),
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
            self._channel_number, code, speed, duration)
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
        """ Handles the service call from SERVICE_SET_DAY_NIGHT_MODE to set the day/night color mode """
        channel = self._logical_channel
        await self._coordinator.client.async_set_video_in_day_night_mode(channel, config_type, mode)
        await self._coordinator.async_refresh()

    async def async_reboot(self):
        """ Handles the service call from SERVICE_REBOOT to reboot the device """
        await self._coordinator.client.reboot()

    async def async_set_record_mode(self, mode: str):
        """ Handles the service call from SERVICE_SET_RECORD_MODE to set the record mode """
        channel = self._logical_channel
        await self._coordinator.client.async_set_record_mode(channel, mode)
        await self._coordinator.async_refresh()

    async def async_set_video_profile_mode(self, mode: str):
        """ Handles the service call from SERVICE_SET_VIDEO_PROFILE_MODE to set profile mode to day/night """
        channel = self._logical_channel
        model = self._coordinator.get_model()
        # Some NVRs like the Lorex DHI-NVR4108HS-8P-4KS2 change the day/night mode through a switch
        if any(substring in model for substring in ['NVR4108HS', 'IPC-Color4K']):
            await self._coordinator.client.async_set_night_switch_mode(channel, mode)
        else:
            await self._coordinator.client.async_set_video_profile_mode(channel, mode)

    async def async_adjustfocus(self, focus: str, zoom: str):
        """ Handles the service call from SERVICE_SET_INFRARED_MODE to set zoom and focus """
        await self._coordinator.client.async_adjustfocus_v1(focus, zoom)
        await self._coordinator.async_refresh()

    async def async_set_privacy_masking(self, index: int, enabled: bool):
        """ Handles the service call from SERVICE_SET_PRIVACY_MASKING to control the privacy masking """
        await self._coordinator.client.async_setprivacymask(index, enabled)

    async def async_set_privacy_mode(self, enabled: bool):
        """ Handles the service call from SERVICE_SET_PRIVACY_MODE to control the lens privacy mask """
        await self._coordinator.client.async_set_privacy_mode(enabled)
        await self._coordinator.async_refresh()

    async def async_set_enable_channel_title(self, enabled: bool):
        """ Handles the service call from SERVICE_ENABLE_CHANNEL_TITLE """
        channel = self._logical_channel
        await self._coordinator.client.async_enable_channel_title(channel, enabled)

    async def async_set_enable_time_overlay(self, enabled: bool):
        """ Handles the service call from SERVICE_ENABLE_TIME_OVERLAY  """
        channel = self._logical_channel
        await self._coordinator.client.async_enable_time_overlay(channel, enabled)

    async def async_set_enable_text_overlay(self, group: int, enabled: bool):
        """ Handles the service call from SERVICE_ENABLE_TEXT_OVERLAY """
        channel = self._logical_channel
        await self._coordinator.client.async_enable_text_overlay(channel, group, enabled)

    async def async_set_enable_custom_overlay(self, group: int, enabled: bool):
        """ Handles the service call from SERVICE_ENABLE_CUSTOM_OVERLAY """
        channel = self._logical_channel
        await self._coordinator.client.async_enable_custom_overlay(channel, group, enabled)

    async def async_set_enable_all_ivs_rules(self, enabled: bool):
        """ Handles the service call from SERVICE_ENABLE_ALL_IVS_RULES """
        channel = self._logical_channel
        await self._coordinator.client.async_set_all_ivs_rules(channel, enabled)

    async def async_enable_ivs_rule(self, index: int, enabled: bool):
        """ Handles the service call from SERVICE_ENABLE_IVS_RULE """
        channel = self._logical_channel
        await self._coordinator.client.async_set_ivs_rule(channel, index, enabled)

    async def async_vto_open_door(self, door_id: int):
        """ Handles the service call from SERVICE_VTO_OPEN_DOOR """
        await self._coordinator.client.async_access_control_open_door(door_id)

    async def async_vto_cancel_call(self):
        """ Handles the service call from SERVICE_VTO_CANCEL_CALL to cancel VTO calls """
        # The service is offered on every camera entity, and only a doorbell
        # ever has a VTO client: on anything else this is None, and so was the
        # error -- AttributeError on NoneType, with a traceback and no clue that
        # the wrong entity had been picked. A doorbell between reconnects lands
        # here too.
        vto_client = self._coordinator.get_vto_client()
        if vto_client is None:
            raise HomeAssistantError(
                "{0} has no doorbell connection to cancel a call on. This service "
                "works on a VTO doorbell, once its event connection is up.".format(
                    self._coordinator.get_device_name()
                )
            )
        try:
            await vto_client.cancel_call()
        except CancelCallRefused as refused:
            raise HomeAssistantError(str(refused)) from refused

    async def async_set_service_set_channel_title(self, text1: str, text2: str):
        """ Handles the service call from SERVICE_SET_CHANNEL_TITLE to set profile mode to day/night """
        channel = self._logical_channel
        await self._coordinator.client.async_set_service_set_channel_title(channel, text1, text2)

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
            "text_overlay": dahua_utils.parse_overlay_lines(data.get(
                "table.VideoWidget[{0}].CustomTitle[{1}].Text".format(
                    channel, group))),
            "custom_overlay": dahua_utils.parse_overlay_lines(data.get(
                "table.VideoWidget[{0}].UserDefinedTitle[{1}].Text".format(
                    channel, group))),
        }

    async def async_set_service_set_text_overlay(self, group: int, text1: str, text2: str, text3: str,
                                                 text4: str):
        """ Handles the service call from SERVICE_SET_TEXT_OVERLAY to set profile mode to day/night """
        channel = self._logical_channel
        await self._coordinator.client.async_set_service_set_text_overlay(channel, group, text1, text2, text3, text4)

    async def async_set_service_set_custom_overlay(self, group: int, text1: str, text2: str):
        """ Handles the service call from SERVICE_SET_CUSTOM_OVERLAY to set profile mode to day/night """
        channel = self._logical_channel
        await self._coordinator.client.async_set_service_set_custom_overlay(channel, group, text1, text2)
