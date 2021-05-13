"""Switch platform for dahua."""
from homeassistant.core import HomeAssistant
from homeassistant.components.switch import SwitchEntity
from custom_components.dahua import DahuaDataUpdateCoordinator

from .const import DOMAIN, MOTION_DETECTION_ICON, SIREN_ICON
from .entity import DahuaBaseEntity
from .client import SIREN_TYPE


async def async_setup_entry(hass: HomeAssistant, entry, async_add_devices):
    """Setup sensor platform."""
    coordinator: DahuaDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]

    devices = []

    # I think most cameras have a motion sensor so we'll blindly add a switch for it
    devices.append(DahuaMotionDetectionBinarySwitch(coordinator, entry))

    # But only some cams have a siren, very few do actually
    if coordinator.supports_siren():
        devices.append(DahuaSirenBinarySwitch(coordinator, entry))

    async_add_devices(devices)


class DahuaMotionDetectionBinarySwitch(DahuaBaseEntity, SwitchEntity):
    """dahua motion detection switch class. Used to enable or disable motion detection"""

    async def async_turn_on(self, **kwargs):  # pylint: disable=unused-argument
        """Turn on/enable motion detection."""
        await self.coordinator.client.enable_motion_detection(True)
        await self.coordinator.async_refresh()

    async def async_turn_off(self, **kwargs):  # pylint: disable=unused-argument
        """Turn off/disable motion detection."""
        await self.coordinator.client.enable_motion_detection(False)
        await self.coordinator.async_refresh()

    @property
    def name(self):
        """Return the name of the switch."""
        return self.coordinator.get_device_name() + " " + "Motion Detection"

    @property
    def unique_id(self):
        """
        A unique identifier for this entity. Needs to be unique within a platform (ie light.hue). Should not be configurable by the user or be changeable
        see https://developers.home-assistant.io/docs/entity_registry_index/#unique-id-requirements
        """
        return self.coordinator.get_serial_number() + "_motion_detection"

    @property
    def icon(self):
        """Return the icon of this switch."""
        return MOTION_DETECTION_ICON

    @property
    def is_on(self):
        """
        Return true if the switch is on.
        Value is fetched from api.get_motion_detection_config
        """
        return self.coordinator.is_motion_detection_enabled()


class DahuaSirenBinarySwitch(DahuaBaseEntity, SwitchEntity):
    """dahua siren switch class. Used to enable or disable camera built in sirens"""

    async def async_turn_on(self, **kwargs):  # pylint: disable=unused-argument
        """Turn on/enable the camrea siren"""
        await self.coordinator.client.async_set_coaxial_control_state(SIREN_TYPE, True)
        await self.coordinator.async_refresh()

    async def async_turn_off(self, **kwargs):  # pylint: disable=unused-argument
        """Turn off/disable camera siren"""
        await self.coordinator.client.async_set_coaxial_control_state(SIREN_TYPE, False)
        await self.coordinator.async_refresh()

    @property
    def name(self):
        """Return the name of the switch."""
        return self.coordinator.get_device_name() + " Siren"

    @property
    def unique_id(self):
        """
        A unique identifier for this entity. Needs to be unique within a platform (ie light.hue). Should not be configurable by the user or be changeable
        see https://developers.home-assistant.io/docs/entity_registry_index/#unique-id-requirements
        """
        return self.coordinator.get_serial_number() + "_siren"

    @property
    def icon(self):
        """Return the icon of this switch."""
        return SIREN_ICON

    @property
    def is_on(self):
        """
        Return true if the siren is on.
        Value is fetched from api.get_motion_detection_config
        """
        return self.coordinator.is_siren_on()
