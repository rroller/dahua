"""
Custom integration to integrate Dahua cameras with Home Assistant.
"""
import asyncio
from typing import Any
import logging
import time
import json

from datetime import timedelta

from aiohttp import ClientError
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, Config, HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_aiohttp_proxy_web, async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.const import EVENT_HOMEASSISTANT_STOP

from custom_components.dahua.thread import DahuaEventThread
from . import dahua_utils
from .client import DahuaClient

from .const import (
    CONF_EVENTS,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_USERNAME,
    CONF_ADDRESS,
    DOMAIN,
    PLATFORMS,
    CONF_RTSP_PORT,
    STARTUP_MESSAGE,
)

SCAN_INTERVAL_SECONDS = timedelta(seconds=30)

_LOGGER: logging.Logger = logging.getLogger(__package__)


async def async_setup(hass: HomeAssistant, config: Config):
    """
    Set up this integration with the UI. YAML is not supported.
    https://developers.home-assistant.io/docs/asyncio_working_with_async/
    """
    hass.data.setdefault(DOMAIN, {})
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Set up this integration using UI."""
    if hass.data.get(DOMAIN) is None:
        hass.data.setdefault(DOMAIN, {})
        _LOGGER.info(STARTUP_MESSAGE)

    username = entry.data.get(CONF_USERNAME)
    password = entry.data.get(CONF_PASSWORD)
    address = entry.data.get(CONF_ADDRESS)
    port = entry.data.get(CONF_PORT)
    rtsp_port = entry.data.get(CONF_RTSP_PORT)
    events = entry.data.get(CONF_EVENTS)

    session = async_get_clientsession(hass)
    dahua_client = DahuaClient(
        username, password, address, port, rtsp_port, session
    )

    coordinator = DahuaDataUpdateCoordinator(hass, dahua_client, events=events)
    await coordinator.async_config_entry_first_refresh()

    if not coordinator.last_update_success:
        raise ConfigEntryNotReady

    hass.data[DOMAIN][entry.entry_id] = coordinator

    # https://developers.home-assistant.io/docs/config_entries_index/
    for platform in PLATFORMS:
        if entry.options.get(platform, True):
            # coordinator.platforms.append(platform)
            hass.async_add_job(
                hass.config_entries.async_forward_entry_setup(entry, platform)
            )

    entry.add_update_listener(async_reload_entry)

    await coordinator.async_start_event_listener()

    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, coordinator.async_stop)
    )

    return True


class DahuaDataUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching data from the API."""

    def __init__(self, hass: HomeAssistant, dahua_client: DahuaClient, events: list) -> None:
        """Initialize."""
        self.client: DahuaClient = dahua_client
        self.dahua_event: DahuaEventThread = None
        self.platforms = []
        self.initialized = False
        self.model = ""
        self.machine_name = ""
        self.connected = None
        self.channels = {"1": "1"}
        self.events: list = events
        self.motion_timestamp_seconds = 0
        self.motion_listener: CALLBACK_TYPE
        self.supports_coaxial_control = False

        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=SCAN_INTERVAL_SECONDS)

    async def async_start_event_listener(self):
        """ setup """
        if self.events is not None:
            self.dahua_event = DahuaEventThread(self.hass, self.machine_name, self.client, self.on_receive, self.events)
            self.dahua_event.start()

    async def async_stop(self, event: Any):
        """ Stop anything we need to stop """
        if self.dahua_event is not None:
            self.dahua_event.stop()

    async def _async_update_data(self):
        """Reload the camera information"""
        try:
            data = {}

            if not self.initialized:
                results = await asyncio.gather(
                    self.client.async_get_system_info(),
                    self.client.async_get_machine_name(),
                    self.client.get_software_version(),
                )

                for result in results:
                    data.update(result)

                device_type = data.get("deviceType")
                if device_type == "IP Camera":
                    # Some firmwares put the device type in the "updateSerial" field. Weird.
                    device_type = data.get("updateSerial")
                data["model"] = device_type
                self.model = device_type
                self.machine_name = data.get("table.General.MachineName")

                try:
                    await self.client.async_get_coaxial_control_io_status()
                    self.supports_coaxial_control = True
                except ClientError as exception:
                    pass

                self.initialized = True

            if self.supports_coaxial_control:
                results = await asyncio.gather(
                    self.client.async_get_coaxial_control_io_status(),
                    self.client.async_common_config(),
                )
            else:
                results = await asyncio.gather(
                    self.client.async_common_config(),
                )

            for result in results:
                data.update(result)

            if self.supports_security_light():
                light_v2 = await self.client.async_get_lighting_v2()
                data.update(light_v2)

            return data
        except Exception as exception:
            raise UpdateFailed() from exception

    def on_receive(self, data_bytes: bytes):
        """
        Takes in bytes from the Dahua event stream, converts to a string, parses to a dict and fires an event with the data on the HA event bus
        Example input:

        b'Code=VideoMotion;action=Start;index=0;data={\n'
        b'   "Id" : [ 0 ],\n'
        b'   "RegionName" : [ "Region1" ]\n'
        b'}\n'
        b'\r\n'


        Example events that are fired on the HA event bus:
        {'name': 'Cam13', 'Code': 'VideoMotion', 'action': 'Start', 'index': '0', 'data': {'Id': [0], 'RegionName': ['Region1'], 'SmartMotionEnable': False}}
        {'name': 'Cam13', 'Code': 'VideoMotion', 'action': 'Stop', 'index': '0', 'data': {'Id': [0], 'RegionName': ['Region1'], 'SmartMotionEnable': False}}
        {
            'name': 'Cam8', 'Code': 'CrossLineDetection', 'action': 'Start', 'index': '0', 'data': {'Class': 'Normal', 'DetectLine': [[18, 4098], [8155, 5549]], 'Direction':      'RightToLeft', 'EventSeq': 40, 'FrameSequence': 549073, 'GroupID': 40, 'Mark': 0, 'Name': 'Rule1', 'Object': {'Action': 'Appear', 'BoundingBox': [4816, 4552, 5248, 5272], 'Center': [5032, 4912], 'Confidence': 0, 'FrameSequence': 0, 'ObjectID': 542, 'ObjectType': 'Unknown', 'RelativeID': 0, 'Source': 0.0, 'Speed': 0, 'SpeedTypeInternal': 0}, 'PTS': 42986015370.0, 'RuleId': 1, 'Source': 51190936.0, 'Track': None, 'UTC': 1620477656, 'UTCMS': 180}
        }
        """
        data = data_bytes.decode("utf-8", errors="ignore")

        for line in data.split("\r\n"):
            if not line.startswith("Code="):
                continue

            event = dict()
            event["name"] = self.get_device_name()
            for key_value in line.split(';'):
                key, value = key_value.split('=')
                event[key] = value

            if event["index"] in self.channels:
                event["channel"] = self.channels[event["index"]]

            # data is a json string, convert it to real json and add it back to the output dic
            if "data" in event:
                try:
                    data = json.loads(event["data"])
                    event["data"] = data
                except Exception:  # pylint: disable=broad-except
                    pass

            # When there's a motion start event we'll set a flag to the current timestmap in seconds.
            # We'll reset it when the motion stops. We'll use this elsewhere to know how long to trigger a motion sensor
            if event["Code"] == "VideoMotion":
                action = event["action"]
                if action == "Start":
                    self.motion_timestamp_seconds = int(time.time())
                    if self.motion_listener:
                        self.motion_listener()
                elif action == "Stop":
                    self.motion_timestamp_seconds = 0
                    if self.motion_listener:
                        self.motion_listener()

            self.hass.bus.fire("dahua_event_received", event)

    def add_motion_listener(self, listener: CALLBACK_TYPE):
        """ Adds the motion listener. This callback will be called on motion events """
        self.motion_listener = listener

    def supports_siren(self) -> bool:
        """
        Returns true if this camera has a siren. For example, the IPC-HDW3849HP-AS-PV does
        https://dahuawiki.com/Template:NameConvention
        """
        return "-AS-PV" in self.model

    def supports_security_light(self) -> bool:
        """
        Returns true if this camera has the red/blud flashing security light feature.  For example, the
        IPC-HDW3849HP-AS-PV does https://dahuawiki.com/Template:NameConvention
        """
        return "-AS-PV" in self.model

    def supports_infrared_light(self) -> bool:
        """
        Returns true if this camera has an infrared light.  For example, the IPC-HDW3849HP-AS-PV does not, but most
        others do. I don't know of a better way to detect this
        """
        return "-AS-PV" not in self.model and "-AS-NI" not in self.model and "-AS-LED" not in self.model

    def supports_illuminator(self) -> bool:
        """
        Returns true if this camera has an illuminator (white light for color cameras).  For example, the
        IPC-HDW3849HP-AS-PV does
        """
        return "table.Lighting_V2[0][0][0].Mode" in self.data

    def is_motion_detection_enabled(self) -> bool:
        """
        Returns true if motion detection is enabled for the camera
        """
        return self.data.get("table.MotionDetect[0].Enable", "").lower() == "true"

    def is_siren_on(self) -> bool:
        """
        Returns true if the camera siren is on
        """
        return self.data.get("status.status.Speaker", "").lower() == "on"

    def get_device_name(self) -> str:
        """ returns the device name, e.g. Cam 2 """
        return self.machine_name

    def get_model(self) -> str:
        """ returns the device model, e.g. IPC-HDW3849HP-AS-PV """
        return self.model

    def get_firmware_version(self) -> str:
        """ returns the device firmware e.g. """
        return self.data.get("version")

    def get_serial_number(self) -> str:
        """ returns the device serial number. This is unique per device """
        return self.data.get("serialNumber")

    def is_infrared_light_on(self) -> bool:
        """ returns true if the infrared light is on """
        return self.data.get("table.Lighting[0][0].Mode", "") == "Manual"

    def get_infrared_brightness(self) -> int:
        """Return the brightness of this light, as reported by the camera itself, between 0..255 inclusive"""

        bri = self.data.get("table.Lighting[0][0].MiddleLight[0].Light")
        return dahua_utils.dahua_brightness_to_hass_brightness(bri)

    def is_illuminator_on(self) -> bool:
        """Return true if the illuminator light is on"""
        return self.data.get("table.Lighting_V2[0][0][0].Mode", "") == "Manual"

    def get_illuminator_brightness(self) -> int:
        """Return the brightness of the illuminator light, as reported by the camera itself, between 0..255 inclusive"""

        bri = self.data.get("table.Lighting_V2[0][0][0].MiddleLight[0].Light")
        return dahua_utils.dahua_brightness_to_hass_brightness(bri)

    def is_security_light_on(self) -> bool:
        """Return true if the security light is on. This is the red/blue flashing light"""
        return self.data.get("status.status.WhiteLight", "") == "On"


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Handle removal of an entry."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    coordinator.dahua_event.stopped.set()
    unloaded = all(
        await asyncio.gather(
            *[
                hass.config_entries.async_forward_entry_unload(entry, platform)
                for platform in PLATFORMS
                if platform in coordinator.platforms
            ]
        )
    )
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id)

    return unloaded


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload config entry."""
    await async_unload_entry(hass, entry)
    await async_setup_entry(hass, entry)
