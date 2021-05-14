"""
Illuminator for for Dahua cameras that have white light illuminators.

See https://developers.home-assistant.io/docs/core/entity/light
"""

from homeassistant.core import HomeAssistant
from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    SUPPORT_BRIGHTNESS,
    LightEntity,
)

from . import DahuaDataUpdateCoordinator, dahua_utils
from .const import DOMAIN, SECURITY_LIGHT_ICON, INFRARED_ICON
from .entity import DahuaBaseEntity
from .client import SECURITY_LIGHT_TYPE

DAHUA_SUPPORTED_OPTIONS = SUPPORT_BRIGHTNESS


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities):
    """Setup light platform."""
    coordinator = hass.data[DOMAIN][entry.entry_id]

    entities = []
    if coordinator.supports_infrared_light():
        entities.append(DahuaInfraredLight(coordinator, entry, "Infrared"))

    if coordinator.supports_illuminator():
        entities.append(DahuaIlluminator(coordinator, entry, "Illuminator"))

    if coordinator.supports_security_light():
        entities.append(DahuaSecurityLight(coordinator, entry, "Security Light"))

    async_add_entities(entities)


class DahuaInfraredLight(DahuaBaseEntity, LightEntity):
    """Representation of a Dahua infrared light (for cameras that have them)"""

    def __init__(self, coordinator: DahuaDataUpdateCoordinator, entry, name):
        super().__init__(coordinator, entry)
        self._name = name

    @property
    def name(self):
        """Return the name of the light."""
        return self.coordinator.get_device_name() + " " + self._name

    @property
    def unique_id(self):
        """
        A unique identifier for this entity. Needs to be unique within a platform (ie light.hue). Should not be configurable by the user or be changeable
        see https://developers.home-assistant.io/docs/entity_registry_index/#unique-id-requirements
        """
        return self.coordinator.get_serial_number() + "_infrared"

    @property
    def is_on(self):
        """Return true if the light is on"""
        return self.coordinator.is_infrared_light_on()

    @property
    def brightness(self):
        """Return the brightness of this light between 0..255 inclusive"""
        return self.coordinator.get_infrared_brightness()

    @property
    def supported_features(self):
        """Flag supported features."""
        return DAHUA_SUPPORTED_OPTIONS

    @property
    def should_poll(self):
        """Don't poll."""
        return False

    async def async_turn_on(self, **kwargs):
        """Turn the light on with the current brightness"""
        hass_brightness = kwargs.get(ATTR_BRIGHTNESS)
        dahua_brightness = dahua_utils.hass_brightness_to_dahua_brightness(hass_brightness)
        await self.coordinator.client.async_set_lighting_v1(True, dahua_brightness)
        await self.coordinator.async_refresh()

    async def async_turn_off(self, **kwargs):
        """Turn the light off"""
        hass_brightness = kwargs.get(ATTR_BRIGHTNESS)
        dahua_brightness = dahua_utils.hass_brightness_to_dahua_brightness(hass_brightness)
        await self.coordinator.client.async_set_lighting_v1(False, dahua_brightness)
        await self.coordinator.async_refresh()

    @property
    def icon(self):
        """Return the icon of this switch."""
        return INFRARED_ICON


class DahuaIlluminator(DahuaBaseEntity, LightEntity):
    """Representation of a Dahua light (for cameras that have them)"""

    def __init__(self, coordinator: DahuaDataUpdateCoordinator, entry, name):
        super().__init__(coordinator, entry)
        self._name = name

    @property
    def name(self):
        """Return the name of the light."""
        return self.coordinator.get_device_name() + " " + self._name

    @property
    def unique_id(self):
        """
        A unique identifier for this entity. Needs to be unique within a platform (ie light.hue). Should not be configurable by the user or be changeable
        see https://developers.home-assistant.io/docs/entity_registry_index/#unique-id-requirements
        """
        return self.coordinator.get_serial_number() + "_illuminator"

    @property
    def is_on(self):
        """Return true if the light is on"""
        return self.coordinator.is_illuminator_on()

    @property
    def brightness(self):
        """Return the brightness of this light between 0..255 inclusive"""

        return self.coordinator.get_illuminator_brightness()

    @property
    def supported_features(self):
        """Flag supported features."""
        return DAHUA_SUPPORTED_OPTIONS

    @property
    def should_poll(self):
        """Don't poll."""
        return False

    async def async_turn_on(self, **kwargs):
        """Turn the light on with the current brightness"""
        hass_brightness = kwargs.get(ATTR_BRIGHTNESS)
        dahua_brightness = dahua_utils.hass_brightness_to_dahua_brightness(hass_brightness)
        await self.coordinator.client.async_set_lighting_v2(True, dahua_brightness)
        await self.coordinator.async_refresh()

    async def async_turn_off(self, **kwargs):
        """Turn the light off"""
        hass_brightness = kwargs.get(ATTR_BRIGHTNESS)
        dahua_brightness = dahua_utils.hass_brightness_to_dahua_brightness(hass_brightness)
        await self.coordinator.client.async_set_lighting_v2(False, dahua_brightness)
        await self.coordinator.async_refresh()


class DahuaSecurityLight(DahuaBaseEntity, LightEntity):
    """Representation of a Dahua light (for cameras that have them). This is the red/blue flashing lights"""

    def __init__(self, coordinator: DahuaDataUpdateCoordinator, entry, name):
        super().__init__(coordinator, entry)
        self._name = name

    @property
    def name(self):
        """Return the name of the light."""
        return self.coordinator.get_device_name() + " " + self._name

    @property
    def unique_id(self):
        """
        A unique identifier for this entity. Needs to be unique within a platform (ie light.hue). Should not be configurable by the user or be changeable
        see https://developers.home-assistant.io/docs/entity_registry_index/#unique-id-requirements
        """
        return self.coordinator.get_serial_number() + "_security"

    @property
    def is_on(self):
        """Return true if the light is on"""
        return self.coordinator.is_security_light_on()

    @property
    def should_poll(self):
        """Don't poll."""
        return False

    async def async_turn_on(self, **kwargs):
        """Turn the light on"""
        await self.coordinator.client.async_set_coaxial_control_state(SECURITY_LIGHT_TYPE, True)
        await self.coordinator.async_refresh()

    async def async_turn_off(self, **kwargs):
        """Turn the light off"""
        await self.coordinator.client.async_set_coaxial_control_state(SECURITY_LIGHT_TYPE, False)
        await self.coordinator.async_refresh()

    @property
    def icon(self):
        """Return the icon of this switch."""
        return SECURITY_LIGHT_ICON
