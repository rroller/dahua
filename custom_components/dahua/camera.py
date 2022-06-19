"""This component provides basic support for Dahua IP cameras."""
from __future__ import annotations

import logging
import voluptuous as vol

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_platform
from homeassistant.components.camera import SUPPORT_STREAM, Camera

from custom_components.dahua import DahuaDataUpdateCoordinator
from custom_components.dahua.entity import DahuaBaseEntity

from .const import (
    DOMAIN,
)

_LOGGER: logging.Logger = logging.getLogger(__package__)

# This service handled setting the infrared mode on the camera to Off, Auto, or Manual... along with the brightness
SERVICE_SET_INFRARED_MODE = "set_infrared_mode"
# This service handles setting the video profile mode to day or night
SERVICE_SET_VIDEO_PROFILE_MODE = "set_video_profile_mode"
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


async def async_setup_entry(hass: HomeAssistant, config_entry, async_add_entities):
    """Add a Dahua IP camera from a config entry."""

    coordinator: DahuaDataUpdateCoordinator = hass.data[DOMAIN][config_entry.entry_id]
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


class DahuaCamera(DahuaBaseEntity, Camera):
    """An implementation of a Dahua IP camera."""

    def __init__(self, coordinator: DahuaDataUpdateCoordinator, stream_index: int, config_entry):
        """Initialize the Dahua camera."""
        DahuaBaseEntity.__init__(self, coordinator, config_entry)
        Camera.__init__(self)

        name = coordinator.client.to_stream_name(stream_index)
        self._channel_number = coordinator.get_channel_number()
        self._coordinator = coordinator
        self._name = "{0} {1}".format(config_entry.title, name)
        self._unique_id = coordinator.get_serial_number() + "_" + name
        self._stream_index = stream_index
        self._motion_status = False
        self._stream_source = coordinator.client.get_rtsp_stream_url(self._channel_number, stream_index)

    @property
    def unique_id(self):
        """Return the entity unique ID."""
        return self._unique_id

    async def async_camera_image(self, width: int | None = None, height: int | None = None):
        """Return a still image response from the camera."""
        # Send the request to snap a picture and return raw jpg data
        return await self._coordinator.client.async_get_snapshot(self._channel_number)

    @property
    def supported_features(self):
        """Return supported features."""
        return SUPPORT_STREAM

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
            channel = self._coordinator.get_channel()
            await self._coordinator.client.enable_motion_detection(channel, True)
            await self._coordinator.async_refresh()
        except TypeError:
            _LOGGER.debug("Failed enabling motion detection on '%s'. Is it supported by the device?", self._name)

    async def async_disable_motion_detection(self):
        """Disable motion detection."""
        try:
            channel = self._coordinator.get_channel()
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
        channel = self._coordinator.get_channel()
        await self._coordinator.client.async_set_lighting_v1_mode(channel, mode, brightness)
        await self._coordinator.async_refresh()

    async def async_set_video_in_day_night_mode(self, config_type: str, mode: str):
        """ Handles the service call from SERVICE_SET_DAY_NIGHT_MODE to set the day/night color mode """
        channel = self._coordinator.get_channel()
        await self._coordinator.client.async_set_video_in_day_night_mode(channel, config_type, mode)
        await self._coordinator.async_refresh()

    async def async_reboot(self):
        """ Handles the service call from SERVICE_REBOOT to reboot the device """
        await self._coordinator.client.reboot()

    async def async_set_record_mode(self, mode: str):
        """ Handles the service call from SERVICE_SET_RECORD_MODE to set the record mode """
        channel = self._coordinator.get_channel()
        await self._coordinator.client.async_set_record_mode(channel, mode)
        await self._coordinator.async_refresh()

    async def async_set_video_profile_mode(self, mode: str):
        """ Handles the service call from SERVICE_SET_VIDEO_PROFILE_MODE to set profile mode to day/night """
        channel = self._coordinator.get_channel()
        model = self._coordinator.get_model()
        # Some NVRs like the Lorex DHI-NVR4108HS-8P-4KS2 change the day/night mode through a switch
        if 'NVR4108HS' in model:
            await self._coordinator.client.async_set_night_switch_mode(channel, mode)
        else:
            await self._coordinator.client.async_set_video_profile_mode(channel, mode)

    async def async_set_enable_channel_title(self, enabled: bool):
        """ Handles the service call from SERVICE_ENABLE_CHANNEL_TITLE """
        channel = self._coordinator.get_channel()
        await self._coordinator.client.async_enable_channel_title(channel, enabled)

    async def async_set_enable_time_overlay(self, enabled: bool):
        """ Handles the service call from SERVICE_ENABLE_TIME_OVERLAY  """
        channel = self._coordinator.get_channel()
        await self._coordinator.client.async_enable_time_overlay(channel, enabled)

    async def async_set_enable_text_overlay(self, group: int, enabled: bool):
        """ Handles the service call from SERVICE_ENABLE_TEXT_OVERLAY """
        channel = self._coordinator.get_channel()
        await self._coordinator.client.async_enable_text_overlay(channel, group, enabled)

    async def async_set_enable_custom_overlay(self, group: int, enabled: bool):
        """ Handles the service call from SERVICE_ENABLE_CUSTOM_OVERLAY """
        channel = self._coordinator.get_channel()
        await self._coordinator.client.async_enable_custom_overlay(channel, group, enabled)

    async def async_set_enable_all_ivs_rules(self, enabled: bool):
        """ Handles the service call from SERVICE_ENABLE_ALL_IVS_RULES """
        channel = self._coordinator.get_channel()
        await self._coordinator.client.async_set_all_ivs_rules(channel, enabled)

    async def async_enable_ivs_rule(self, index: int, enabled: bool):
        """ Handles the service call from SERVICE_ENABLE_IVS_RULE """
        channel = self._coordinator.get_channel()
        await self._coordinator.client.async_set_ivs_rule(channel, index, enabled)

    async def async_vto_open_door(self, door_id: int):
        """ Handles the service call from SERVICE_VTO_OPEN_DOOR """
        await self._coordinator.client.async_access_control_open_door(door_id)

    async def async_vto_cancel_call(self):
        """ Handles the service call from SERVICE_VTO_CANCEL_CALL to cancel VTO calls """
        await self._coordinator.get_vto_client().cancel_call()

    async def async_set_service_set_channel_title(self, text1: str, text2: str):
        """ Handles the service call from SERVICE_SET_CHANNEL_TITLE to set profile mode to day/night """
        channel = self._coordinator.get_channel()
        await self._coordinator.client.async_set_service_set_channel_title(channel, text1, text2)

    async def async_set_service_set_text_overlay(self, group: int, text1: str, text2: str, text3: str,
                                                 text4: str):
        """ Handles the service call from SERVICE_SET_TEXT_OVERLAY to set profile mode to day/night """
        channel = self._coordinator.get_channel()
        await self._coordinator.client.async_set_service_set_text_overlay(channel, group, text1, text2, text3, text4)

    async def async_set_service_set_custom_overlay(self, group: int, text1: str, text2: str):
        """ Handles the service call from SERVICE_SET_CUSTOM_OVERLAY to set profile mode to day/night """
        channel = self._coordinator.get_channel()
        await self._coordinator.client.async_set_service_set_custom_overlay(channel, group, text1, text2)
