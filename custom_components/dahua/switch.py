"""Switch platform for dahua."""
from aiohttp import ClientError
from homeassistant.core import HomeAssistant
from homeassistant.components.switch import SwitchEntity
from custom_components.dahua import DahuaDataUpdateCoordinator

from .const import DOMAIN, DISARMING_ICON, MOTION_DETECTION_ICON, SIREN_ICON
from .entity import DahuaBaseEntity
from .client import SIREN_TYPE


async def async_setup_entry(hass: HomeAssistant, entry, async_add_devices):
    """Setup sensor platform."""
    coordinator: DahuaDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]

    # I think most cameras have a motion sensor so we'll blindly add a switch for it
    devices = [
        DahuaMotionDetectionBinarySwitch(coordinator, entry),
    ]

    # But only some cams have a siren, very few do actually
    if coordinator.supports_siren():
        devices.append(DahuaSirenBinarySwitch(coordinator, entry))
    if coordinator.supports_smart_motion_detection() or coordinator.supports_smart_motion_detection_amcrest():
        devices.append(DahuaSmartMotionDetectionBinarySwitch(coordinator, entry))

    try:
        await coordinator.client.async_get_disarming_linkage()
        devices.append(DahuaDisarmingLinkageBinarySwitch(coordinator, entry))
    except ClientError as exception:
        pass

    async_add_devices(devices)


class DahuaMotionDetectionBinarySwitch(DahuaBaseEntity, SwitchEntity):
    """dahua motion detection switch class. Used to enable or disable motion detection"""

    async def async_turn_on(self, **kwargs):  # pylint: disable=unused-argument
        """Turn on/enable motion detection."""
        channel = self._coordinator.get_channel()
        await self._coordinator.client.enable_motion_detection(channel, True)
        await self._coordinator.async_refresh()

    async def async_turn_off(self, **kwargs):  # pylint: disable=unused-argument
        """Turn off/disable motion detection."""
        channel = self._coordinator.get_channel()
        await self._coordinator.client.enable_motion_detection(channel, False)
        await self._coordinator.async_refresh()

    @property
    def name(self):
        """Return the name of the switch."""
        return self._coordinator.get_device_name() + " " + "Motion Detection"

    @property
    def unique_id(self):
        """
        A unique identifier for this entity. Needs to be unique within a platform (ie light.hue). Should not be configurable by the user or be changeable
        see https://developers.home-assistant.io/docs/entity_registry_index/#unique-id-requirements
        """
        return self._coordinator.get_serial_number() + "_motion_detection"

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
        return self._coordinator.is_motion_detection_enabled()


class DahuaDisarmingLinkageBinarySwitch(DahuaBaseEntity, SwitchEntity):
    """will set the camera's disarming linkage (Event -> Disarming in the UI)"""

    async def async_turn_on(self, **kwargs):  # pylint: disable=unused-argument
        """Turn on/enable linkage"""
        channel = self._coordinator.get_channel()
        await self._coordinator.client.async_set_disarming_linkage(channel, True)
        await self._coordinator.async_refresh()

    async def async_turn_off(self, **kwargs):  # pylint: disable=unused-argument
        """Turn off/disable linkage"""
        channel = self._coordinator.get_channel()
        await self._coordinator.client.async_set_disarming_linkage(channel, False)
        await self._coordinator.async_refresh()

    @property
    def name(self):
        """Return the name of the switch."""
        return self._coordinator.get_device_name() + " " + "Disarming"

    @property
    def unique_id(self):
        """
        A unique identifier for this entity. Needs to be unique within a platform (ie light.hue). Should not be
        configurable by the user or be changeable see
        https://developers.home-assistant.io/docs/entity_registry_index/#unique-id-requirements
        """
        return self._coordinator.get_serial_number() + "_disarming"

    @property
    def icon(self):
        """Return the icon of this switch."""
        return DISARMING_ICON

    @property
    def is_on(self):
        """
        Return true if the switch is on.
        Value is fetched from client.async_get_linkage
        """
        return self._coordinator.is_disarming_linkage_enabled()


class DahuaSmartMotionDetectionBinarySwitch(DahuaBaseEntity, SwitchEntity):
    """Enables or disables the Smart Motion Detection option in the camera"""

    async def async_turn_on(self, **kwargs):  # pylint: disable=unused-argument
        """Turn on SmartMotionDetect"""
        if self._coordinator.supports_smart_motion_detection_amcrest():
            await self._coordinator.client.async_set_ivs_rule(0, 0, True)
        else:
            await self._coordinator.client.async_enabled_smart_motion_detection(True)
        await self._coordinator.async_refresh()

    async def async_turn_off(self, **kwargs):  # pylint: disable=unused-argument
        """Turn off SmartMotionDetect"""
        if self._coordinator.supports_smart_motion_detection_amcrest():
            await self._coordinator.client.async_set_ivs_rule(0, 0, False)
        else:
            await self._coordinator.client.async_enabled_smart_motion_detection(False)
        await self._coordinator.async_refresh()

    @property
    def name(self):
        """Return the name of the switch."""
        return self._coordinator.get_device_name() + " " + "Smart Motion Detection"

    @property
    def unique_id(self):
        """
        A unique identifier for this entity. Needs to be unique within a platform (ie light.hue). Should not be
        configurable by the user or be changeable see
        https://developers.home-assistant.io/docs/entity_registry_index/#unique-id-requirements
        """
        return self._coordinator.get_serial_number() + "_smart_motion_detection"

    @property
    def icon(self):
        """Return the icon of this switch."""
        return MOTION_DETECTION_ICON

    @property
    def is_on(self):
        """ Return true if the switch is on. """
        return self._coordinator.is_smart_motion_detection_enabled()


class DahuaSirenBinarySwitch(DahuaBaseEntity, SwitchEntity):
    """dahua siren switch class. Used to enable or disable camera built in sirens"""

    async def async_turn_on(self, **kwargs):  # pylint: disable=unused-argument
        """Turn on/enable the camera's siren"""
        channel = self._coordinator.get_channel()
        await self._coordinator.client.async_set_coaxial_control_state(channel, SIREN_TYPE, True)
        await self._coordinator.async_refresh()

    async def async_turn_off(self, **kwargs):  # pylint: disable=unused-argument
        """Turn off/disable camera siren"""
        channel = self._coordinator.get_channel()
        await self._coordinator.client.async_set_coaxial_control_state(channel, SIREN_TYPE, False)
        await self._coordinator.async_refresh()

    @property
    def name(self):
        """Return the name of the switch."""
        return self._coordinator.get_device_name() + " Siren"

    @property
    def unique_id(self):
        """
        A unique identifier for this entity. Needs to be unique within a platform (ie light.hue). Should not be configurable by the user or be changeable
        see https://developers.home-assistant.io/docs/entity_registry_index/#unique-id-requirements
        """
        return self._coordinator.get_serial_number() + "_siren"

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
        return self._coordinator.is_siren_on()
