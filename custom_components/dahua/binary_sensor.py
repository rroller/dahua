"""Binary sensor platform for dahua."""
import re
import time

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.core import HomeAssistant
from custom_components.dahua import DahuaDataUpdateCoordinator

from .const import (
    MOTION_SENSOR_DEVICE_CLASS,
    DOMAIN, SAFETY_DEVICE_CLASS, CONNECTIVITY_DEVICE_CLASS, SOUND_DEVICE_CLASS, DOOR_DEVICE_CLASS, NONE_DEVICE_CLASS,
)
from .entity import DahuaBaseEntity

# Override event names. Otherwise we'll generate the name from the event name for example SmartMotionHuman will
# become "Smart Motion Human"
# BackKeyLight States
# State   | Description
# 0       | OK, No Call/Ring
# 1, 2    | Call/Ring
# 4       | Voice message
# 5       | Call answered from VTH
# 6       | Call not answered
# 7       | VTH calling VTO
# 8       | Unlock
# 9       | Unlock failed
# 11      | Device rebooted
NAME_OVERRIDES = {
    "VideoMotion": "Motion Alarm",
    "CrossLineDetection": "Cross Line Alarm",
    "BackKeyLight": "Button Pressed",  # For VTO devices (doorbells)
    "BackKeyLight-4": "Voice Message Event",  # For VTO devices (doorbells)
    "BackKeyLight-5": "Call Answered From VTH Event",  # For VTO devices (doorbells)
    "BackKeyLight-6": "Call Not Answered Event",  # For VTO devices (doorbells)
    "BackKeyLight-7": "VTH Calling VTO Event",  # For VTO devices (doorbells)
    "BackKeyLight-8": "Unlock Event",  # For VTO devices (doorbells)
    "BackKeyLight-9": "Unlock Failed Event",  # For VTO devices (doorbells)
    "BackKeyLight-11": "Device Rebooted Event",  # For VTO devices (doorbells)
}

# Override the device class for events
DEVICE_CLASS_OVERRIDES = {
    "VideoMotion": MOTION_SENSOR_DEVICE_CLASS,
    "CrossLineDetection": MOTION_SENSOR_DEVICE_CLASS,
    "AlarmLocal": SAFETY_DEVICE_CLASS,
    "VideoLoss": SAFETY_DEVICE_CLASS,
    "VideoBlind": SAFETY_DEVICE_CLASS,
    "StorageNotExist": CONNECTIVITY_DEVICE_CLASS,
    "StorageFailure": CONNECTIVITY_DEVICE_CLASS,
    "StorageLowSpace": SAFETY_DEVICE_CLASS,
    "FireWarning": SAFETY_DEVICE_CLASS,
    "BackKeyLight": SOUND_DEVICE_CLASS,
    "DoorStatus": DOOR_DEVICE_CLASS,
    "BackKeyLight-4": NONE_DEVICE_CLASS,  # "Voice message",
    "BackKeyLight-5": NONE_DEVICE_CLASS,  # "Call answered from VTH",
    "BackKeyLight-6": NONE_DEVICE_CLASS,  # "Call not answered",
    "BackKeyLight-7": NONE_DEVICE_CLASS,  # "VTH calling VTO",
    "BackKeyLight-8": NONE_DEVICE_CLASS,
    "BackKeyLight-9": SAFETY_DEVICE_CLASS,
    "BackKeyLight-11": NONE_DEVICE_CLASS,  # "Device rebooted",
}


async def async_setup_entry(hass: HomeAssistant, entry, async_add_devices):
    """Setup binary_sensor platform."""
    coordinator: DahuaDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]

    sensors: list[DahuaEventSensor] = []
    for event_name in coordinator.get_event_list():
        sensors.append(DahuaEventSensor(coordinator, entry, event_name))

    # For doorbells we'll just add these since most people will want them
    if coordinator.is_doorbell():
        sensors.append(DahuaEventSensor(coordinator, entry, "BackKeyLight"))
        sensors.append(DahuaEventSensor(coordinator, entry, "BackKeyLight-4"))  # Voice Message
        sensors.append(DahuaEventSensor(coordinator, entry, "BackKeyLight-5"))  # Call answered from VTH
        sensors.append(DahuaEventSensor(coordinator, entry, "BackKeyLight-6"))  # Call not answered
        sensors.append(DahuaEventSensor(coordinator, entry, "BackKeyLight-7"))  # VTH calling VTO
        sensors.append(DahuaEventSensor(coordinator, entry, "BackKeyLight-8"))  # Unlock
        sensors.append(DahuaEventSensor(coordinator, entry, "BackKeyLight-9"))  # Unlock failed
        sensors.append(DahuaEventSensor(coordinator, entry, "BackKeyLight-11"))  # Device rebooted
        sensors.append(DahuaEventSensor(coordinator, entry, "Invite"))
        sensors.append(DahuaEventSensor(coordinator, entry, "DoorStatus"))
        sensors.append(DahuaEventSensor(coordinator, entry, "CallNoAnswered"))

    if sensors:
        async_add_devices(sensors)


class DahuaEventSensor(DahuaBaseEntity, BinarySensorEntity):
    """
    dahua binary_sensor class to record events. Many of these events are configured in the camera UI by going to:
    Setting -> Event -> IVS -> and adding a tripwire rule, etc. See the DahuaEventThread in thread.py on how we connect
    to the camera to listen to events.
    """

    def __init__(self, coordinator: DahuaDataUpdateCoordinator, config_entry, event_name: str):
        DahuaBaseEntity.__init__(self, coordinator, config_entry)
        BinarySensorEntity.__init__(self)

        # event_name is the event name, example: VideoMotion, CrossLineDetection, SmartMotionHuman, etc
        self._event_name = event_name

        self._coordinator = coordinator
        self._device_name = coordinator.get_device_name()
        self._device_class = DEVICE_CLASS_OVERRIDES.get(event_name, MOTION_SENSOR_DEVICE_CLASS)

        # name is the friendly name, example: Cross Line Alarm. If the name is not found in the override it will be
        # generated from the event_name. For example SmartMotionHuman willbecome "Smart Motion Human"
        # https://stackoverflow.com/questions/25674532/pythonic-way-to-add-space-before-capital-letter-if-and-only-if-previous-letter-i/25674575
        default_name = re.sub(r"(?<![A-Z])(?<!^)([A-Z])", r" \1", event_name)
        self._name = NAME_OVERRIDES.get(event_name, default_name)

        # Build the unique ID. This will convert the name to lower underscores. For example, "Smart Motion Vehicle" will
        # become "smart_motion_vehicle" and will be added as a suffix to the device serial number
        self._unique_id = coordinator.get_serial_number() + "_" + self._name.lower().replace(" ", "_")
        if event_name == "VideoMotion":
            # We need this for backwards compatibility as the VideoMotion was created with a unique ID of just the
            # serial number and we don't want to break people who are upgrading
            self._unique_id = coordinator.get_serial_number()

    @property
    def unique_id(self):
        """Return the entity unique ID."""
        return self._unique_id

    @property
    def name(self):
        """Return the name of the binary_sensor. Example: Cam14 Motion Alarm"""
        return f"{self._device_name} {self._name}"

    @property
    def device_class(self):
        """Return the class of this binary_sensor, Example: motion"""
        return self._device_class

    @property
    def is_on(self):
        """
        Return true if the event is activated.

        This is the magic part of this sensor along with the async_added_to_hass method below.
        The async_added_to_hass method adds a listener to the coordinator so when the event is started or stopped
        it calls the async_write_ha_state function. async_write_ha_state gets the current value from this is_on method.
        """
        event_timestamp = self._coordinator.get_event_timestamp(self._event_name)
        now = int(time.time())
        if self._event_name.startswith("BackKeyLight-") and event_timestamp > 0 and (now - event_timestamp) >= 30:
            return False

        return event_timestamp > 0

    async def async_added_to_hass(self):
        """Connect to dispatcher listening for entity data notifications."""
        self._coordinator.add_dahua_event_listener(self._event_name, self.async_write_ha_state)

    @property
    def should_poll(self) -> bool:
        """Return True if entity has to be polled for state.  False if entity pushes its state to HA"""
        return False
