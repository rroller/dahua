"""
Custom integration to integrate Dahua cameras with Home Assistant.
"""
import asyncio
from typing import Any, Dict
import logging
import ssl
import time

from datetime import timedelta

from homeassistant.components.tag import async_scan_tag
import hashlib

from aiohttp import ClientError, ClientResponseError, ClientConnectorError, ClientSession, TCPConnector
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, Config, HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady, PlatformNotReady
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.const import EVENT_HOMEASSISTANT_STOP

from custom_components.dahua.thread import DahuaEventThread, DahuaVtoEventThread
from . import dahua_utils
from .client import DahuaClient

from .const import (
    CONF_EVENTS,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_USERNAME,
    CONF_ADDRESS,
    CONF_NAME,
    DOMAIN,
    PLATFORMS,
    CONF_RTSP_PORT,
    STARTUP_MESSAGE,
    CONF_CHANNEL,
)
from .dahua_utils import parse_event
from .vto import DahuaVTOClient

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
    port = int(entry.data.get(CONF_PORT))
    rtsp_port = int(entry.data.get(CONF_RTSP_PORT))
    events = entry.data.get(CONF_EVENTS)
    name = entry.data.get(CONF_NAME)
    channel = entry.data.get(CONF_CHANNEL, 0)

    coordinator = DahuaDataUpdateCoordinator(hass, events=events, address=address, port=port, rtsp_port=rtsp_port,
                                             username=username, password=password, name=name, channel=channel)
    await coordinator.async_config_entry_first_refresh()

    if not coordinator.last_update_success:
        _LOGGER.warning("dahua async_setup_entry for init, data not ready")
        raise ConfigEntryNotReady

    hass.data[DOMAIN][entry.entry_id] = coordinator

    # https://developers.home-assistant.io/docs/config_entries_index/
    for platform in PLATFORMS:
        if entry.options.get(platform, True):
            coordinator.platforms.append(platform)
            hass.async_add_job(
                hass.config_entries.async_forward_entry_setup(entry, platform)
            )

    entry.add_update_listener(async_reload_entry)

    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, coordinator.async_stop)
    )

    return True


class DahuaDataUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching data from the API."""

    def __init__(self, hass: HomeAssistant, events: list, address: str, port: int, rtsp_port: int, username: str,
                 password: str, name: str, channel: int) -> None:
        """Initialize the coordinator."""
        # Self signed certs are used over HTTPS so we'll disable SSL verification
        ssl_context = ssl.create_default_context()
        ssl_context.set_ciphers("DEFAULT")
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE
        connector = TCPConnector(enable_cleanup_closed=True, ssl=ssl_context)
        self._session = ClientSession(connector=connector)

        # The client used to communicate with Dahua devices
        self.client: DahuaClient = DahuaClient(username, password, address, port, rtsp_port, self._session)

        self.platforms = []
        self.initialized = False
        self.model = ""
        self.connected = None
        self.events: list = events
        self._supports_coaxial_control = False
        self._supports_disarming_linkage = False
        self._supports_smart_motion_detection = False
        self._supports_lighting = False
        self._serial_number: str
        self._profile_mode = "0"
        self._supports_profile_mode = False
        self._channel = channel
        self._address = address
        self._max_streams = 3  # 1 main stream + 2 sub-streams by default

        # channel_number is not the channel_index. channel_number is the index + 1.
        # So channel index 0 is channel number 1. Except for some older firmwares where channel
        # and channel number are the same! We check for this in _async_update_data and adjust the
        # channel number as needed.
        self._channel_number = channel + 1

        # This is the name for the device given by the user during setup
        self._name = name

        # This is the name as reported from the camera itself
        self.machine_name = ""

        # This thread is what connects to the cameras event stream and fires on_receive when there's an event
        self.dahua_event_thread = DahuaEventThread(hass, self.client, self.on_receive, events, self._channel)

        # This thread will connect to VTO devices (Dahua doorbells)
        self.dahua_vto_event_thread = DahuaVtoEventThread(hass, self.client, self.on_receive_vto_event, host=address,
                                                          port=5000, username=username, password=password)

        # A dictionary of event name (CrossLineDetection, VideoMotion, etc) to a listener for that event
        # The key will be formed from self.get_event_key(event_name) and includes the channel
        self._dahua_event_listeners: Dict[str, CALLBACK_TYPE] = dict()

        # A dictionary of event name (CrossLineDetection, VideoMotion, etc) to the time the event fire or was cleared.
        # If cleared the time will be 0. The time unit is seconds epoch
        self._dahua_event_timestamp: Dict[str, int] = dict()

        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=SCAN_INTERVAL_SECONDS)

    async def async_start_event_listener(self):
        """ Starts the event listeners for IP cameras (this does not work for doorbells (VTO)) """
        if self.events is not None:
            self.dahua_event_thread.start()

    async def async_start_vto_event_listener(self):
        """ Starts the event listeners for doorbells (VTO). This will not work for IP cameras"""
        if self.dahua_vto_event_thread is not None:
            self.dahua_vto_event_thread.start()

    async def async_stop(self, event: Any):
        """ Stop anything we need to stop """
        self.dahua_event_thread.stop()
        self.dahua_vto_event_thread.stop()
        await self._close_session()

    async def _close_session(self) -> None:
        _LOGGER.debug("Closing Session")
        if self._session != None:
            try:
                await self._session.close()
                self._session = None
            except Exception as e:
                _LOGGER.exception("serverConnect - failed to close session")

    async def _async_update_data(self):
        try:
            data = await self._async_update_data_int()
            return data
        except Exception as ex:
            # If we let an exception bubble up, it seems to result in self being
            # deleted. So clean up first.
            await self._close_session()
            raise
    
    async def _async_update_data_int(self):
        """Reload the camera information"""
        data = {}

        # Do the one time initialization (do this when Home Assistant starts)
        if not self.initialized:
            try:
                try:
                    await self.client.async_get_snapshot(0)
                    # If able to take a snapshot with index 0 then most likely this cams channel needs to be reset
                    self._channel_number = self._channel
                except ClientError:
                    pass
                _LOGGER.info("Using channel number %s", self._channel_number)

                # Find the max number of streams. 1 main stream + n number of sub-streams
                self._max_streams = await self.client.get_max_extra_streams() + 1
                _LOGGER.info("Using max streams %s", self._max_streams)

                machine_name = await self.client.async_get_machine_name()
                sys_info = await self.client.async_get_system_info()
                version = await self.client.get_software_version()
                data.update(machine_name)
                data.update(sys_info)
                data.update(version)

                device_type = data.get("deviceType", None)
                # Lorex NVRs return deviceType=31, but the model is in the updateSerial
                # /cgi-bin/magicBox.cgi?action=getSystemInfo"
                # deviceType=31
                # processor=ST7108
                # serialNumber=ND0219110NNNNN
                # updateSerial=DHI-NVR4108HS-8P-4KS2
                if device_type in ["IP Camera", "31"] or device_type is None:
                    # Some firmwares put the device type in the "updateSerial" field. Weird.
                    device_type = data.get("updateSerial", None)
                    if device_type is None:
                        # If it's still none, then call the device type API
                        dt = await self.client.get_device_type()
                        device_type = dt.get("type")
                data["model"] = device_type
                self.model = device_type
                self.machine_name = data.get("table.General.MachineName")
                self._serial_number = data.get("serialNumber")

                try:
                    await self.client.async_get_coaxial_control_io_status()
                    self._supports_coaxial_control = True
                except ClientResponseError:
                    self._supports_coaxial_control = False
                _LOGGER.info("Device supports Coaxial Control=%s", self._supports_coaxial_control)

                try:
                    await self.client.async_get_disarming_linkage()
                    self._supports_disarming_linkage = True
                except ClientError:
                    self._supports_disarming_linkage = False
                _LOGGER.info("Device supports disarming linkage=%s", self._supports_disarming_linkage)

                # Smart motion detection is enabled/disabled/fetched differently on Dahua devices compared to Amcrest
                # The following lines are for Dahua devices
                try:
                    await self.client.async_get_smart_motion_detection()
                    self._supports_smart_motion_detection = True
                except ClientError:
                    self._supports_smart_motion_detection = False
                _LOGGER.info("Device supports smart motion detection=%s", self._supports_smart_motion_detection)

                is_doorbell = self.is_doorbell()
                _LOGGER.info("Device is a doorbell=%s", is_doorbell)

                is_amcrest_flood_light = self.is_amcrest_flood_light()
                _LOGGER.info("Device is an Amcrest floodlight=%s",is_amcrest_flood_light)

                try:
                    await self.client.async_get_config_lighting(self._channel, self._profile_mode)
                    self._supports_lighting = True
                except ClientError:
                    self._supports_lighting = False
                    pass
                _LOGGER.info("Device supports infrared lighting=%s",self.supports_infrared_light())

                if not is_doorbell:
                    # Start the event listeners for IP cameras
                    await self.async_start_event_listener()

                    try:
                        # Some cams don't support profile modes, check and see... use 2 to check
                        conf = await self.client.async_get_config("Lighting[0][2]")
                        # We'll get back an error like this if it doesn't work:
                        # Error: Error -1 getting param in name=Lighting[0][1]
                        # Otherwise we'll get multiple lines of config back
                        self._supports_profile_mode = len(conf) > 1
                    except ClientError:
                        _LOGGER.info("Cam does not support profile mode. Will use mode 0")
                        self._supports_profile_mode = False
                    _LOGGER.info("Device supports profile mode=%s", self._supports_profile_mode)
                else:
                    # Start the event listeners for doorbells (VTO)
                    await self.async_start_vto_event_listener()

                self.initialized = True
            except (ClientConnectorError, asyncio.TimeoutError) as exception:
                _LOGGER.warning(exception)
                # Pass the exception on up. Our caller
                # homeassistant/helpers/update_coordinator.py:_async_refresh()
                # gracefully handles some common errors like timeout and connection errors.
                raise
            except Exception as exception:
                _LOGGER.error("Failed to initialize device at %s", self._address, exc_info=exception)
                raise PlatformNotReady("Dahua device at " + self._address + " isn't fully initialized yet") from exception

        # This is the event loop code that's called every n seconds
        try:
            # We need the profile mode (0=day, 1=night, 2=scene)
            if self._supports_profile_mode and not self.is_doorbell():
                try:
                    mode_data = await self.client.async_get_video_in_mode()
                    data.update(mode_data)
                    self._profile_mode = mode_data.get("table.VideoInMode[0].Config[0]", "0")
                    if not self._profile_mode:
                        self._profile_mode = "0"
                except Exception as exception:
                    # I believe this API is missing on some cameras so we'll just ignore it and move on
                    _LOGGER.debug("Could not get profile mode", exc_info=exception)
                    pass

            # Figure out which APIs we need to call and then fan out and gather the results
            coros = [
                asyncio.ensure_future(self.client.async_get_config_motion_detection()),
            ]
            if self.supports_infrared_light():
                coros.append(
                    asyncio.ensure_future(self.client.async_get_config_lighting(self._channel, self._profile_mode)))
            if self._supports_disarming_linkage:
                coros.append(asyncio.ensure_future(self.client.async_get_disarming_linkage()))
            if self._supports_coaxial_control:
                coros.append(asyncio.ensure_future(self.client.async_get_coaxial_control_io_status()))
            if self._supports_smart_motion_detection:
                coros.append(asyncio.ensure_future(self.client.async_get_smart_motion_detection()))
            if self.supports_smart_motion_detection_amcrest():
                coros.append(asyncio.ensure_future(self.client.async_get_video_analyse_rules_for_amcrest()))
            if self.is_amcrest_doorbell():
                coros.append(asyncio.ensure_future(self.client.async_get_light_global_enabled()))

            # Gather results and update the data map
            results = await asyncio.gather(*coros)
            for result in results:
                if result is not None:
                    data.update(result)

            if self.supports_security_light() or self.is_amcrest_flood_light():
                light_v2 = await self.client.async_get_lighting_v2()
                if light_v2 is not None:
                    data.update(light_v2)

            return data
        except Exception as exception:
            _LOGGER.warning("Failed to sync device state for %s. See README to enable debug logs to get full exception",
                            self._address)
            _LOGGER.debug("Failed to sync device state for %s", self._address, exc_info=exception)
            raise UpdateFailed() from exception

    def on_receive_vto_event(self, event: dict):
        event["DeviceName"] = self.get_device_name()
        _LOGGER.debug(f"VTO Data received: {event}")
        self.hass.bus.fire("dahua_event_received", event)

        # Example events:
        # {
        #   "Code":"VideoMotion",
        #   "Action":"Start",
        #   "Data":{
        #     "LocaleTime":"2021-06-19 15:36:58",
        #     "UTC":1624088218.0
        # }
        #
        # {
        #   "Code":"DoorStatus",
        #   "Action":"Pulse",
        #   "Data":{
        #      "LocaleTime":"2021-04-11 21:34:52",
        #      "Status":"Close",
        #      "UTC":1618148092
        #    },
        #    "Index":0
        # }
        #
        # {
        #    "Code":"BackKeyLight",
        #    "Action":"Pulse",
        #    "Data":{
        #       "LocaleTime":"2021-06-20 13:52:20",
        #       "State":1,
        #       "UTC":1624168340.0
        #    },
        #    "Index":-1
        # }

        # This is the event code, example: VideoMotion, CrossLineDetection, BackKeyLight, PhoneCallDetect, DoorStatus, etc
        code = self.translate_event_code(event)
        event_key = self.get_event_key(code)

        if code == "AccessControl":
            card_id = event.get("Data", {}).get("CardNo", "")
            if card_id:
                card_id_md5 = hashlib.md5(card_id.encode()).hexdigest()
                asyncio.run_coroutine_threadsafe(
                    async_scan_tag(self.hass, card_id_md5, self.get_device_name()), self.hass.loop
                ).result()

        listener = self._dahua_event_listeners.get(event_key)
        if listener is not None:
            action = event.get("Action", "")
            if action == "Start":
                self._dahua_event_timestamp[event_key] = int(time.time())
                listener()
            elif action == "Stop":
                self._dahua_event_timestamp[event_key] = 0
                listener()
            elif action == "Pulse":
                if code == "DoorStatus":
                    if event.get("Data", {}).get("Status", "") == "Open":
                        self._dahua_event_timestamp[event_key] = int(time.time())
                    else:
                        self._dahua_event_timestamp[event_key] = 0
                else:
                    state = event.get("Data", {}).get("State", 0)
                    if state == 1:
                        # button pressed
                        self._dahua_event_timestamp[event_key] = int(time.time())
                    else:
                        self._dahua_event_timestamp[event_key] = 0
                listener()

    def on_receive(self, data_bytes: bytes, channel: int):
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
        events = parse_event(data)

        if len(events) == 0:
            return

        _LOGGER.debug(f"Events received from {self.get_address()} on channel {channel}: {events}")

        for event in events:
            index = 0
            if "index" in event:
                try:
                    index = int(event["index"])
                except ValueError:
                    index = 0

            # This is a short term fix. Right now for NVRs this integration creates a thread per channel to listen to events. Every thread gets the same response. We need to
            # discard events not for this channel. Longer term work should create only a single thread per channel.
            if index != self._channel:
                continue

            # Put the vent on the HA event bus
            event["name"] = self.get_device_name()
            event["DeviceName"] = self.get_device_name()
            self.hass.bus.fire("dahua_event_received", event)

            # When there's an event start we'll update the a map x to the current timestamp in seconds for the event.
            # We'll reset it to 0 when the event stops.
            # We'll use these timestamps in binary_sensor to know how long to trigger the sensor

            # This is the event code, example: VideoMotion, CrossLineDetection, etc
            event_name = self.translate_event_code(event)

            event_key = self.get_event_key(event_name)
            listener = self._dahua_event_listeners.get(event_key)
            if listener is not None:
                action = event["action"]
                if action == "Start":
                    self._dahua_event_timestamp[event_key] = int(time.time())
                    listener()
                elif action == "Stop":
                    self._dahua_event_timestamp[event_key] = 0
                    listener()

    def translate_event_code(self, event: dict):
        """
        translate_event_code will try to convert the event code to a less specific event code if the device doesn't have a listener for the more specific type
        Example event codes: VideoMotion, CrossLineDetection, BackKeyLight, DoorStatus
        """
        code = event.get("Code", "")

        # For CrossLineDetection, the event data will look like this... and if there's a human detected then we'll use the SmartMotionHuman code instead
        # {
        #    "Code": "CrossLineDetection",
        #    "Data": {
        #        "Object": {
        #            "ObjectType": "Human",
        #        }
        #    }
        # }
        if code == "CrossLineDetection" or code == "CrossRegionDetection":
            data = event.get("data", event.get("Data", {}))
            is_human = data.get("Object", {}).get("ObjectType", "").lower() == "human"
            if is_human and self._dahua_event_listeners.get(self.get_event_key(code)) is None:
                return "SmartMotionHuman"

        # Convert doorbell pressed related events to common event name, DoorbellPressed.
        # VTO devices will use the event BackKeyLight and the Amcrest devices seem to use PhoneCallDetect
        if code == "BackKeyLight" or code == "PhoneCallDetect":
            code = "DoorbellPressed"

        return code

    def get_event_timestamp(self, event_name: str) -> int:
        """
        Returns the event timestamp. If the event is firing then it will be the time of the firing. Otherwise returns 0.
        event_name: the event name, example: CrossLineDetection
        """
        event_key = self.get_event_key(event_name)
        return self._dahua_event_timestamp.get(event_key, 0)

    def add_dahua_event_listener(self, event_name: str, listener: CALLBACK_TYPE):
        """ Adds an event listener for the given event (CrossLineDetection, etc).
        This callback will be called when the event fire """
        event_key = self.get_event_key(event_name)
        self._dahua_event_listeners[event_key] = listener

    def supports_siren(self) -> bool:
        """
        Returns true if this camera has a siren. For example, the IPC-HDW3849HP-AS-PV does
        https://dahuawiki.com/Template:NameConvention
        """
        return "-AS-PV" in self.model

    def supports_security_light(self) -> bool:
        """
        Returns true if this camera has the red/blue flashing security light feature.  For example, the
        IPC-HDW3849HP-AS-PV does https://dahuawiki.com/Template:NameConvention
        """
        return "-AS-PV" in self.model or self.model == "AD410"

    def is_doorbell(self) -> bool:
        """ Returns true if this is a doorbell (VTO) """
        m = self.model.upper()
        return m.startswith("VTO") or m.startswith("DH-VTO") or ("NVR" not in m and m.startswith("DHI")) or self.is_amcrest_doorbell()

    def is_amcrest_doorbell(self) -> bool:
        """ Returns true if this is an Amcrest doorbell """
        return self.model.upper().startswith("AD")

    def is_amcrest_flood_light(self) -> bool:
        """ Returns true if this camera is an Amcrest Floodlight camera (eg.ASH26-W) """
        return self.model.upper().startswith("ASH26")

    def supports_infrared_light(self) -> bool:
        """
        Returns true if this camera has an infrared light.  For example, the IPC-HDW3849HP-AS-PV does not, but most
        others do. I don't know of a better way to detect this
        """
        if not self._supports_lighting:
            return False
        return "-AS-PV" not in self.model and "-AS-NI" not in self.model

    def supports_illuminator(self) -> bool:
        """
        Returns true if this camera has an illuminator (white light for color cameras).  For example, the
        IPC-HDW3849HP-AS-PV does
        """
        return not ( self.is_amcrest_doorbell() or self.is_amcrest_flood_light() ) and "table.Lighting_V2[{0}][0][0].Mode".format(self._channel) in self.data

    def is_motion_detection_enabled(self) -> bool:
        """ Returns true if motion detection is enabled for the camera """
        return self.data.get("table.MotionDetect[{0}].Enable".format(self._channel), "").lower() == "true"

    def is_disarming_linkage_enabled(self) -> bool:
        """ Returns true if disarming linkage is enable """
        return self.data.get("table.DisableLinkage.Enable", "").lower() == "true"

    def is_smart_motion_detection_enabled(self) -> bool:
        """ Returns true if smart motion detection is enabled """
        if self.supports_smart_motion_detection_amcrest():
            return self.data.get("table.VideoAnalyseRule[0][0].Enable", "").lower() == "true"
        else:
            return self.data.get("table.SmartMotionDetect[0].Enable", "").lower() == "true"

    def is_siren_on(self) -> bool:
        """ Returns true if the camera siren is on """
        return self.data.get("status.status.Speaker", "").lower() == "on"

    def get_device_name(self) -> str:
        """ returns the device name, e.g. Cam 2 """
        if self._name is not None:
            return self._name
        # Earlier releases of this integration didn't allow for setting the camera name, it always used the machine name
        # Now we fall back to the machine name if that wasn't supplied at config time.
        return self.machine_name

    def get_model(self) -> str:
        """ returns the device model, e.g. IPC-HDW3849HP-AS-PV """
        return self.model

    def get_firmware_version(self) -> str:
        """ returns the device firmware e.g. """
        return self.data.get("version")

    def get_serial_number(self) -> str:
        """ returns the device serial number. This is unique per device """
        if self._channel > 0:
            # We need a unique identifier. For NVRs we get back the same serial, so add the channel to the end of the sn
            return "{0}_{1}".format(self._serial_number, self._channel)
        return self._serial_number

    def get_event_list(self) -> list:
        """
        Returns the list of events selected when configuring the camera in Home Assistant. For example:
        [VideoMotion, VideoLoss, CrossLineDetection]
        """
        return self.events

    def is_infrared_light_on(self) -> bool:
        """ returns true if the infrared light is on """
        return self.data.get("table.Lighting[{0}][0].Mode".format(self._channel), "") == "Manual"

    def get_infrared_brightness(self) -> int:
        """Return the brightness of this light, as reported by the camera itself, between 0..255 inclusive"""

        bri = self.data.get("table.Lighting[{0}][0].MiddleLight[0].Light".format(self._channel))
        return dahua_utils.dahua_brightness_to_hass_brightness(bri)

    def is_illuminator_on(self) -> bool:
        """Return true if the illuminator light is on"""
        # profile_mode 0=day, 1=night, 2=scene
        profile_mode = self.get_profile_mode()

        return self.data.get("table.Lighting_V2[{0}][{1}][0].Mode".format(self._channel, profile_mode), "") == "Manual"

    def is_amcrest_flood_light_on(self) -> bool:
        """Return true if the amcrest flood light light is on"""
        # profile_mode 0=day, 1=night, 2=scene
        profile_mode = self.get_profile_mode()

        return self.data.get(f'table.Lighting_V2[{self._channel}][{profile_mode}][1].Mode') == "Manual"

    def is_ring_light_on(self) -> bool:
        """Return true if ring light is on for an Amcrest Doorbell"""
        return self.data.get("table.LightGlobal[0].Enable") == "true"

    def get_illuminator_brightness(self) -> int:
        """Return the brightness of the illuminator light, as reported by the camera itself, between 0..255 inclusive"""

        bri = self.data.get("table.Lighting_V2[{0}][0][0].MiddleLight[0].Light".format(self._channel))
        return dahua_utils.dahua_brightness_to_hass_brightness(bri)

    def is_security_light_on(self) -> bool:
        """Return true if the security light is on. This is the red/blue flashing light"""
        return self.data.get("status.status.WhiteLight", "") == "On"

    def get_profile_mode(self) -> str:
        # profile_mode 0=day, 1=night, 2=scene
        return self._profile_mode

    def get_channel(self) -> int:
        """returns the channel index of this camera. 0 based. Channel index 0 is channel number 1"""
        return self._channel

    def get_channel_number(self) -> int:
        """returns the channel number of this camera"""
        return self._channel_number

    def get_event_key(self, event_name: str) -> str:
        """returns the event key we use for listeners. It uses the channel index to support multiple channels"""
        return "{0}-{1}".format(event_name, self._channel)

    def get_address(self) -> str:
        """returns the IP address of this camera"""
        return self._address

    def get_max_streams(self) -> int:
        """Returns the max number of streams supported by the device. All streams might not be enabled though"""
        return self._max_streams

    def supports_smart_motion_detection(self) -> bool:
        """ True if smart motion detection is supported"""
        return self._supports_smart_motion_detection

    def supports_smart_motion_detection_amcrest(self) -> bool:
        """ True if smart motion detection is supported for an amcrest device"""
        return self.model == "AD410"

    def get_vto_client(self) -> DahuaVTOClient:
        """
        Returns an instance of the connected VTO client if this is a VTO device. We need this because there's different
        ways to call a VTO device and the VTO client will handle that. For example, to hang up a call
        """
        return self.dahua_vto_event_thread.vto_client


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Handle removal of an entry."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    coordinator.dahua_event_thread.stop()
    coordinator.dahua_vto_event_thread.stop()
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
