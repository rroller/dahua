"""Binary sensor platform for dahua."""
from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.core import HomeAssistant
from custom_components.dahua import DahuaDataUpdateCoordinator

from .const import (
    MOTION_SENSOR_DEVICE_CLASS,
    DOMAIN,
)
from .entity import DahuaBaseEntity


async def async_setup_entry(hass: HomeAssistant, entry, async_add_devices):
    """Setup binary_sensor platform."""
    coordinator = hass.data[DOMAIN][entry.entry_id]

    async_add_devices([
        DahuaMotionSensor(coordinator, entry),
        DahuaCrossLineDetectionSensor(coordinator, entry)
    ])


class DahuaMotionSensor(DahuaBaseEntity, BinarySensorEntity):
    """dahua binary_sensor class to record motion events"""

    def __init__(self, coordinator: DahuaDataUpdateCoordinator, config_entry):
        DahuaBaseEntity.__init__(self, coordinator, config_entry)
        BinarySensorEntity.__init__(self)

        self._name = coordinator.get_device_name()
        self._coordinator = coordinator
        self._unique_id = coordinator.get_serial_number() + "_motion_alarm"

    @property
    def name(self):
        """Return the name of the binary_sensor."""
        return f"{self._name} Motion Alarm"

    @property
    def device_class(self):
        """Return the class of this binary_sensor."""
        return MOTION_SENSOR_DEVICE_CLASS

    @property
    def is_on(self):
        """
        Return true if motion is activated.

        This is the magic part of this sensor along with the async_added_to_hass method below.
        The async_added_to_hass method adds a listener to the coordinator so when motion is started or stopped
        it calls the async_write_ha_state function. async_write_ha_state just gets the current value from this is_on method.
        """
        return self._coordinator.motion_timestamp_seconds > 0

    async def async_added_to_hass(self):
        """Connect to dispatcher listening for entity data notifications."""
        self._coordinator.add_motion_listener(self.async_write_ha_state)

    @property
    def should_poll(self) -> bool:
        """Return True if entity has to be polled for state.  False if entity pushes its state to HA"""
        return False


class DahuaCrossLineDetectionSensor(DahuaBaseEntity, BinarySensorEntity):
    """
    dahua binary_sensor class to record 'cross line detection' events. This is also known as 'tripwire'. This is
    configured in the camera UI by going to Setting -> Event -> IVS -> and adding a tripwire rule.
    """

    def __init__(self, coordinator: DahuaDataUpdateCoordinator, config_entry):
        DahuaBaseEntity.__init__(self, coordinator, config_entry)
        BinarySensorEntity.__init__(self)

        self._name = coordinator.get_device_name()
        self._coordinator = coordinator
        self._unique_id = coordinator.get_serial_number() + "_cross_line_alarm"

    @ property
    def unique_id(self):
        """Return the entity unique ID."""
        return self._unique_id

    @property
    def name(self):
        """Return the name of the binary_sensor."""
        return f"{self._name} Cross Line Alarm"

    @property
    def device_class(self):
        """Return the class of this binary_sensor."""
        return MOTION_SENSOR_DEVICE_CLASS

    @property
    def is_on(self):
        """
        Return true if a cross line detection event activated.

        This is the magic part of this sensor along with the async_added_to_hass method below.
        The async_added_to_hass method adds a listener to the coordinator so when the event is started or stopped
        it calls the async_write_ha_state function. async_write_ha_state just gets the current value from this is_on method.
        """
        return self._coordinator.cross_line_detection_timestamp_seconds > 0

    async def async_added_to_hass(self):
        """Connect to dispatcher listening for entity data notifications."""
        self._coordinator.add_cross_line_detection_listener(self.async_write_ha_state)

    @property
    def should_poll(self) -> bool:
        """Return True if entity has to be polled for state.  False if entity pushes its state to HA"""
        return False
