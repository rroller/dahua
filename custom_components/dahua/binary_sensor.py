"""Binary sensor platform for dahua."""
import re

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.core import HomeAssistant
from custom_components.dahua import DahuaDataUpdateCoordinator

from .const import (
    MOTION_SENSOR_DEVICE_CLASS,
    DOMAIN, SAFETY_DEVICE_CLASS, CONNECTIVITY_DEVICE_CLASS, SOUND_DEVICE_CLASS, DOOR_DEVICE_CLASS, VOLUME_HIGH_ICON,
)
from .entity import DahuaBaseEntity

# Override event names. Otherwise we'll generate the name from the event name for example SmartMotionHuman will
# become "Smart Motion Human"
NAME_OVERRIDES = {
    "VideoMotion": "Motion Alarm",
    "CrossLineDetection": "Cross Line Alarm",
    "DoorbellPressed": "Button Pressed",  # For VTO/Doorbell devices
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
    "DoorbellPressed": SOUND_DEVICE_CLASS,
    "DoorStatus": DOOR_DEVICE_CLASS,
    "AudioMutation": SOUND_DEVICE_CLASS,
}

ICON_OVERRIDES = {
    "AudioAnomaly": VOLUME_HIGH_ICON,
    "AudioMutation": VOLUME_HIGH_ICON,
}


async def async_setup_entry(hass: HomeAssistant, entry, async_add_devices):
    """Setup binary_sensor platform."""
    coordinator: DahuaDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]

    sensors: list[DahuaEventSensor] = []
    for event_name in coordinator.get_event_list():
        sensors.append(DahuaEventSensor(coordinator, entry, event_name))

    # For doorbells we'll just add these since most people will want them
    if coordinator.is_doorbell():
        sensors.append(DahuaEventSensor(coordinator, entry, "DoorbellPressed"))
        sensors.append(DahuaEventSensor(coordinator, entry, "Invite"))
        sensors.append(DahuaEventSensor(coordinator, entry, "DoorStatus"))
        sensors.append(DahuaEventSensor(coordinator, entry, "CallNoAnswered"))

    if sensors:
        async_add_devices(sensors)


class DahuaEventSensor(DahuaBaseEntity, BinarySensorEntity):
    """
    dahua binary_sensor class to record events. Many of these events are configured in the camera UI by going to:
    Setting -> Event -> IVS -> and adding a tripwire rule, etc. See the DahuaEventThread in thread.py on how we connect
    to the cammera to listen to events.
    """

    def __init__(self, coordinator: DahuaDataUpdateCoordinator, config_entry, event_name: str):
        DahuaBaseEntity.__init__(self, coordinator, config_entry)
        BinarySensorEntity.__init__(self)

        # event_name is the event name, example: VideoMotion, CrossLineDetection, SmartMotionHuman, etc
        self._event_name = event_name

        self._coordinator = coordinator
        self._device_name = coordinator.get_device_name()
        self._device_class = DEVICE_CLASS_OVERRIDES.get(event_name, MOTION_SENSOR_DEVICE_CLASS)
        self._icon_override = ICON_OVERRIDES.get(event_name, None)

        # name is the friendly name, example: Cross Line Alarm. If the name is not found in the override it will be
        # generated from the event_name. For example SmartMotionHuman will become "Smart Motion Human"
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
    def icon(self) -> str:
        return self._icon_override

    @property
    def is_on(self):
        """
        Return true if the event is activated.

        This is the magic part of this sensor along with the async_added_to_hass method below.
        The async_added_to_hass method adds a listener to the coordinator so when the event is started or stopped
        it calls the async_write_ha_state function. async_write_ha_state gets the current value from this is_on method.
        """
        return self._coordinator.get_event_timestamp(self._event_name) > 0

    async def async_added_to_hass(self):
        """Connect to dispatcher listening for entity data notifications."""
        self._coordinator.add_dahua_event_listener(self._event_name, self.async_write_ha_state)

    @property
    def should_poll(self) -> bool:
        """Return True if entity has to be polled for state.  False if entity pushes its state to HA"""
        return False
