"""
Custom integration to integrate Dahua cameras with Home Assistant.
Patched to:
- Skip PTZ status probe on NVRs and add timeout handling.
- Load only supported platforms (skip 'switch' if no coaxial control; skip 'light' if no lighting).
"""
import asyncio
from typing import Any, Dict
import logging
import ssl
import time

from datetime import timedelta

from homeassistant.components.tag import async_scan_tag
import hashlib

from aiohttp import ClientError, ClientResponseError, ClientSession, TCPConnector
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady, PlatformNotReady
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType
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

SSL_CONTEXT = ssl.create_default_context()
SSL_CONTEXT.set_ciphers("DEFAULT")
SSL_CONTEXT.check_hostname = False
SSL_CONTEXT.verify_mode = ssl.CERT_NONE

_LOGGER: logging.Logger = logging.getLogger(__package__)

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

    coordinator = DahuaDataUpdateCoordinator(
        hass,
        events=events,
        address=address,
        port=port,
        rtsp_port=rtsp_port,
        username=username,
        password=password,
        name=name,
        channel=channel,
    )

    # First refresh (capability detection happens here)
    await coordinator.async_config_entry_first_refresh()

    if not coordinator.last_update_success:
        _LOGGER.warning("dahua async_setup_entry: initial data not ready")
        raise ConfigEntryNotReady

    # Decide which platforms to actually load based on detected support
    # This prevents hanging/cancelled setups on devices that don't implement certain APIs.
    forward_platforms: list[str] = []
    for platform in PLATFORMS:
        # honor Options flow toggles if present
        if not entry.options.get(platform, True):
            continue

        if platform == "switch" and not coordinator._supports_coaxial_control:
            _LOGGER.info("Skipping 'switch' platform (no coaxial control on this device).")
            continue

        if platform == "light" and not (
            coordinator._supports_lighting
            or coordinator._supports_lighting_v2
            or coordinator.supports_security_light()
            or coordinator.is_flood_light()
        ):
            _LOGGER.info("Skipping 'light' platform (no lighting support on this device).")
            continue

        forward_platforms.append(platform)

    hass.data[DOMAIN][entry.entry_id] = coordinator
    coordinator.platforms.extend(forward_platforms)

    if forward_platforms:
        await hass.config_entries.async_forward_entry_setups(entry, forward_platforms)

    entry.add_update_listener(async_reload_entry)

    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, coordinator.async_stop)
    )

    return True


class DahuaDataUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching data from the API."""

    def __init__(
        self,
        hass: HomeAssistant,
        events: list,
        address: str,
        port: int,
        rtsp_port: int,
        username: str,
        password: str,
        name: str,
        channel: int,
    ) -> None:
        """Initialize the coordinator."""
        connector = TCPConnector(enable_cleanup_closed=True, ssl=SSL_CONTEXT)
        self._session = ClientSession(connector=connector)

        # The client used to communicate with Dahua devices
        self.client: DahuaClient = DahuaClient(
            username, password, address, port, rtsp_port, self._session
        )

        self.platforms = []
        self.initialized = False
        self.model = ""
        self.connected = None
        self.events: list = events
        self._supports_coaxial_control = False
        self._supports_disarming_linkage = False
        self._supports_event_notifications = False
        self._supports_smart_motion_detection = False
        self._supports_ptz_position = False
        self._supports_lighting = False
        self._supports_floodlightmode = False
        self._serial_number: str
        self._profile_mode = "0"
        self._preset_position = "0"
        self._supports_profile_mode = False
        self._channel = channel
        self._address = address
        self._max_streams = 3  # 1 main stream + 2 sub-streams by default

        self._supports_lighting_v2 = False

        # channel_number is not the channel_index (index 0 == number 1)
        self._channel_number = channel + 1

        # Name given by the user during setup
        self._name = name
        # Name reported by the device
        self.machine_name = ""

        # Threads for events
        self.dahua_event_thread = DahuaEventThread(
            hass, self.client, self.on_receive, events, self._channel
        )
        self.dahua_vto_event_thread = DahuaVtoEventThread(
            hass,
            self.client,
            self.on_receive_vto_event,
            host=address,
            port=5000,
            username=username,
            password=password,
        )

        self._dahua_event_listeners: Dict[str, CALLBACK_TYPE] = dict()
        self._dahua_event_timestamp: Dict[str, int] = dict()
        self._floodlight_mode = 2

        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=SCAN_INTERVAL_SECONDS)

    async def async_start_event_listener(self):
        """Start event listeners for IP cameras (not doorbells)."""
        if self.events is not None:
            self.dahua_event_thread.start()

    async def async_start_vto_event_listener(self):
        """Start event listeners for doorbells (VTO)."""
        if self.dahua_vto_event_thread is not None:
            self.dahua_vto_event_thread.start()

    async def async_stop(self, event: Any):
        """Stop threads and close session."""
        self.dahua_event_thread.stop()
        self.dahua_vto_event_thread.stop()
        await self._close_session()

    async def _close_session(self) -> None:
        _LOGGER.debug("Closing HTTP session")
        if self._session is not None:
            try:
                await self._session.close()
            except Exception:
                _LOGGER.exception("Failed to close session")
            finally:
                self._session = None

    async def _async_update_data(self):
        """Fetch and refresh device data."""
        data = {}

        if not self.initialized:
            try:
                # Streams
                self._max_streams = await self.client.get_max_extra_streams() + 1
                _LOGGER.info("Using max streams %s", self._max_streams)

                # Identity / versions
                machine_name = await self.client.async_get_machine_name()
                sys_info = await self.client.async_get_system_info()
                version = await self.client.get_software_version()
                data.update(machine_name)
                data.update(sys_info)
                data.update(version)

                device_type = data.get("deviceType", None)
                if device_type in ["IP Camera", "31"] or device_type is None:
                    dt = data.get("updateSerial")
                    if dt is None:
                        dt_map = await self.client.get_device_type()
                        dt = dt_map.get("type")
                    device_type = dt
                data["model"] = device_type
                self.model = device_type
                self.machine_name = data.get("table.General.MachineName")
                self._serial_number = data.get("serialNumber")

                # Channel sanity
                try:
                    await self.client.async_get_snapshot(0)
                    if not self.is_doorbell():
                        self._channel_number = self._channel
                except ClientError:
                    pass
                _LOGGER.info("Using channel number %s", self._channel_number)

                # Feature probes (wrap in try/except so missing APIs don't kill setup)
                try:
                    await self.client.async_get_coaxial_control_io_status()
                    self._supports_coaxial_control = True
                except ClientResponseError:
                    self._supports_coaxial_control = False
                _LOGGER.info("Coaxial Control supported = %s", self._supports_coaxial_control)

                try:
                    await self.client.async_get_disarming_linkage()
                    self._supports_disarming_linkage = True
                except ClientError:
                    self._supports_disarming_linkage = False
                _LOGGER.info("Disarming linkage supported = %s", self._supports_disarming_linkage)

                try:
                    await self.client.async_get_event_notifications()
                    self._supports_event_notifications = True
                except ClientError:
                    self._supports_event_notifications = False
                _LOGGER.info("Event notifications supported = %s", self._supports_event_notifications)

                # ---- PTZ position probe (patched) ----
                self._supports_ptz_position = False
                if "NVR" not in str(self.model).upper():
                    try:
                        await asyncio.wait_for(self.client.async_get_ptz_position(), timeout=5)
                        self._supports_ptz_position = True
                    except (ClientError, ClientResponseError, asyncio.TimeoutError, asyncio.CancelledError) as e:
                        _LOGGER.debug("PTZ position not supported/reachable on %s: %s", self._address, e)
                else:
                    _LOGGER.debug("Skipping PTZ position probe on NVR model: %s", self.model)
                _LOGGER.info("PTZ position supported = %s", self._supports_ptz_position)
                # --------------------------------------

                try:
                    await self.client.async_get_smart_motion_detection()
                    self._supports_smart_motion_detection = True
                except ClientError:
                    self._supports_smart_motion_detection = False
                _LOGGER.info("Smart motion detection supported = %s", self._supports_smart_motion_detection)

                is_doorbell = self.is_doorbell()
                _LOGGER.info("Device is doorbell = %s", is_doorbell)

                is_flood_light = self.is_flood_light()
                _LOGGER.info("Device is floodlight = %s", is_flood_light)

                self._supports_floodlightmode = self.supports_floodlightmode()

                try:
                    await self.client.async_get_config_lighting(self._channel, self._profile_mode)
                    self._supports_lighting = True
                except ClientError:
                    self._supports_lighting = False
                _LOGGER.info("Infrared/lighting supported = %s", self._supports_lighting)

                try:
                    await self.client.async_get_lighting_v2()
                    self._supports_lighting_v2 = True
                except ClientError:
                    self._supports_lighting_v2 = False
                _LOGGER.info("Lighting_V2 supported = %s", self._supports_lighting_v2)

                if not is_doorbell:
                    await self.async_start_event_listener()
                    try:
                        conf = await self.client.async_get_config("Lighting[0][2]")
                        self._supports_profile_mode = len(conf) > 1
                    except ClientError:
                        _LOGGER.info("Cam does not support profile mode; using mode 0")
                        self._supports_profile_mode = False
                    _LOGGER.info("Profile mode supported = %s", self._supports_profile_mode)
                else:
                    await self.async_start_vto_event_listener()

                self.initialized = True

            except Exception as exception:
                _LOGGER.error(
                    "Failed to initialize device at %s", self._address, exc_info=exception
                )
                raise PlatformNotReady(
                    f"Dahua device at {self._address} isn't fully initialized yet"
                )

        # Periodic refresh
        try:
            if self._supports_profile_mode and not self.is_doorbell():
                try:
                    mode_data = await self.client.async_get_video_in_mode()
                    data.update(mode_data)
                    self._profile_mode = mode_data.get("table.VideoInMode[0].Config[0]", "0") or "0"
                except Exception as exception:
                    _LOGGER.debug("Could not get profile mode", exc_info=exception)

            if self._supports_ptz_position:
                try:
                    ptz_data = await self.client.async_get_ptz_position()
                    data.update(ptz_data)
                    self._preset_position = ptz_data.get("status.PresetID", "0") or "0"
                except Exception as exception:
                    _LOGGER.debug("Could not get preset position", exc_info=exception)

            coros = [asyncio.ensure_future(self.client.async_get_config_motion_detection())]
            if self.supports_infrared_light():
                coros.append(
                    asyncio.ensure_future(
                        self.client.async_get_config_lighting(self._channel, self._profile_mode)
                    )
                )
            if self._supports_disarming_linkage:
                coros.append(asyncio.ensure_future(self.client.async_get_disarming_linkage()))
            if self._supports_event_notifications:
                coros.append(asyncio.ensure_future(self.client.async_get_event_notifications()))
            if self._supports_coaxial_control:
                coros.append(asyncio.ensure_future(self.client.async_get_coaxial_control_io_status()))
            if self._supports_smart_motion_detection:
                coros.append(asyncio.ensure_future(self.client.async_get_smart_motion_detection()))
            if self.supports_smart_motion_detection_amcrest():
                coros.append(asyncio.ensure_future(self.client.async_get_video_analyse_rules_for_amcrest()))
            if self.is_amcrest_doorbell():
                coros.append(asyncio.ensure_future(self.client.async_get_light_global_enabled()))
            if self._supports_lighting_v2:
                coros.append(asyncio.ensure_future(self.client.async_get_lighting_v2()))

            results = await asyncio.gather(*coros, return_exceptions=False)
            for result in results:
                if result is not None:
                    data.update(result)

            if self.supports_security_light() or self.is_flood_light():
                light_v2 = await self.client.async_get_lighting_v2()
                if light_v2 is not None:
                    data.update(light_v2)

            return data

        except Exception as exception:
            _LOGGER.warning(
                "Failed to sync device state for %s. Enable debug logs to see the full exception.",
                self._address,
            )
            _LOGGER.debug("Failed to sync device state for %s", self._address, exc_info=exception)
            raise UpdateFailed() from exception

    def on_receive_vto_event(self, event: dict):
        event["DeviceName"] = self.get_device_name()
        _LOGGER.debug(f"VTO Data received: {event}")
        self.hass.bus.fire("dahua_event_received", event)

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
                    self._dahua_event_timestamp[event_key] = int(time.time()) if state == 1 else 0
                listener()

    def on_receive(self, data_bytes: bytes, channel: int):
        """Parse event stream bytes and fire events on HA bus."""
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

            # For NVRs create one thread per channel; discard non-matching channel events.
            if index != self._channel:
                continue

            event["name"] = self.get_device_name()
            event["DeviceName"] = self.get_device_name()
            self.hass.bus.fire("dahua_event_received", event)

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
        code = event.get("Code", "")
        if code in ("CrossLineDetection", "CrossRegionDetection"):
            data = event.get("data", event.get("Data", {}))
            is_human = data.get("Object", {}).get("ObjectType", "").lower() == "human"
            if is_human and self._dahua_event_listeners.get(self.get_event_key(code)) is None:
                return "SmartMotionHuman"
        if code in ("BackKeyLight", "PhoneCallDetect"):
            code = "DoorbellPressed"
        return code

    def get_event_timestamp(self, event_name: str) -> int:
        event_key = self.get_event_key(event_name)
        return self._dahua_event_timestamp.get(event_key, 0)

    def add_dahua_event_listener(self, event_name: str, listener: CALLBACK_TYPE):
        event_key = self.get_event_key(event_name)
        self._dahua_event_listeners[event_key] = listener

    def supports_siren(self) -> bool:
        m = self.model.upper()
        return "-AS-PV" in m or "L46N" in m or m.startswith("W452ASD")

    def supports_security_light(self) -> bool:
        return "-AS-PV" in self.model or self.model == "AD410" or self.model == "DB61i" or self.model.startswith("IP8M-2796E")

    def is_doorbell(self) -> bool:
        m = self.model.upper()
        return m.startswith("VTO") or m.startswith("DH-VTO") or (
            "NVR" not in m and m.startswith("DHI")) or self.is_amcrest_doorbell() or self.is_empiretech_doorbell() or self.is_avaloidgoliath_doorbell()

    def is_amcrest_doorbell(self) -> bool:
        return self.model.upper().startswith("AD") or self.model.upper().startswith("DB6")

    def is_empiretech_doorbell(self) -> bool:
        return self.model.upper().startswith("DB2X")

    def is_avaloidgoliath_doorbell(self) -> bool:
        return self.model.upper().startswith("AV-V")

    def is_flood_light(self) -> bool:
        m = self.model.upper()
        return m.startswith("ASH26") or "L26N" in m or "L46N" in m or m.startswith("V261LC") or m.startswith("W452ASD")

    def supports_infrared_light(self) -> bool:
        if not self._supports_lighting:
            return False
        return "-AS-PV" not in self.model and "-AS-NI" not in self.model and "LED-S2" not in self.model

    def supports_floodlightmode(self) -> bool:
        return "W452ASD" in self.model.upper() or "L46N" in self.model.upper()

    def is_motion_detection_enabled(self) -> bool:
        return self.data.get(f"table.MotionDetect[{self._channel}].Enable", "").lower() == "true"

    def is_disarming_linkage_enabled(self) -> bool:
        return self.data.get("table.DisableLinkage.Enable", "").lower() == "true"

    def is_event_notifications_enabled(self) -> bool:
        return self.data.get("table.DisableEventNotify.Enable", "").lower() == "false"

    def is_smart_motion_detection_enabled(self) -> bool:
        if self.supports_smart_motion_detection_amcrest():
            return self.data.get("table.VideoAnalyseRule[0][0].Enable", "").lower() == "true"
        else:
            return self.data.get("table.SmartMotionDetect[0].Enable", "").lower() == "true"

    def is_siren_on(self) -> bool:
        return self.data.get("status.status.Speaker", "").lower() == "on"

    def get_device_name(self) -> str:
        if self._name is not None:
            return self._name
        return self.machine_name

    def get_model(self) -> str:
        return self.model

    def get_firmware_version(self) -> str:
        return self.data.get("version")

    def get_serial_number(self) -> str:
        if self._channel > 0:
            return f"{self._serial_number}_{self._channel}"
        return self._serial_number

    def get_event_list(self) -> list:
        return self.events

    def is_infrared_light_on(self) -> bool:
        return self.data.get(f"table.Lighting[{self._channel}][0].Mode", "") == "Manual"

    def get_infrared_brightness(self) -> int:
        bri = self.data.get(f"table.Lighting[{self._channel}][0].MiddleLight[0].Light")
        return dahua_utils.dahua_brightness_to_hass_brightness(bri)

    def is_illuminator_on(self) -> bool:
        profile_mode = self.get_profile_mode()
        return self.data.get(
            f"table.Lighting_V2[{self._channel}][{profile_mode}][0].Mode", ""
        ) == "Manual"

    def is_flood_light_on(self) -> bool:
        if self._supports_floodlightmode:
            return self.data.get("status.status.WhiteLight", "") == "On"
        else:
            profile_mode = self.get_profile_mode()
            return self.data.get(
                f"table.Lighting_V2[{self._channel}][{profile_mode}][1].Mode"
            ) == "Manual"

    def is_ring_light_on(self) -> bool:
        return self.data.get("table.LightGlobal[0].Enable") == "true"

    def get_illuminator_brightness(self) -> int:
        bri = self.data.get(
            f"table.Lighting_V2[{self._channel}][0][0].MiddleLight[0].Light"
        )
        return dahua_utils.dahua_brightness_to_hass_brightness(bri)

    def is_security_light_on(self) -> bool:
        return self.data.get("status.status.WhiteLight", "") == "On"

    def get_profile_mode(self) -> str:
        return self._profile_mode

    def get_channel(self) -> int:
        return self._channel

    def get_channel_number(self) -> int:
        return self._channel_number

    def get_event_key(self, event_name: str) -> str:
        return f"{event_name}-{self._channel}"

    def get_address(self) -> str:
        return self._address

    def get_max_streams(self) -> int:
        return self._max_streams

    def supports_smart_motion_detection(self) -> bool:
        return self._supports_smart_motion_detection

    def supports_smart_motion_detection_amcrest(self) -> bool:
        return self.model == "AD410" or self.model == "DB61i"

    def get_vto_client(self) -> DahuaVTOClient:
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
                for platform in coordinator.platforms
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
