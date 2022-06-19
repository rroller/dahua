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

    if coordinator.is_amcrest_flood_light():
        entities.append(AmcrestFloodLight(coordinator, entry, "Flood Light"))

    if coordinator.supports_security_light() and not coordinator.is_amcrest_doorbell():
        #  The Amcrest doorbell works a little different and is added in select.py
        entities.append(DahuaSecurityLight(coordinator, entry, "Security Light"))

    if coordinator.is_amcrest_doorbell():
        entities.append(AmcrestRingLight(coordinator, entry, "Ring Light"))

    async_add_entities(entities)


class DahuaInfraredLight(DahuaBaseEntity, LightEntity):
    """Representation of a Dahua infrared light (for cameras that have them)"""

    def __init__(self, coordinator: DahuaDataUpdateCoordinator, entry, name):
        super().__init__(coordinator, entry)
        self._name = name
        self._coordinator = coordinator

    @property
    def name(self):
        """Return the name of the light."""
        return self._coordinator.get_device_name() + " " + self._name

    @property
    def unique_id(self):
        """
        A unique identifier for this entity. Needs to be unique within a platform (ie light.hue). Should not be configurable by the user or be changeable
        see https://developers.home-assistant.io/docs/entity_registry_index/#unique-id-requirements
        """
        return self._coordinator.get_serial_number() + "_infrared"

    @property
    def is_on(self):
        """Return true if the light is on"""
        return self._coordinator.is_infrared_light_on()

    @property
    def brightness(self):
        """Return the brightness of this light between 0..255 inclusive"""
        return self._coordinator.get_infrared_brightness()

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
        channel = self._coordinator.get_channel()
        await self._coordinator.client.async_set_lighting_v1(channel, True, dahua_brightness)
        await self.coordinator.async_refresh()

    async def async_turn_off(self, **kwargs):
        """Turn the light off"""
        hass_brightness = kwargs.get(ATTR_BRIGHTNESS)
        dahua_brightness = dahua_utils.hass_brightness_to_dahua_brightness(hass_brightness)
        channel = self._coordinator.get_channel()
        await self._coordinator.client.async_set_lighting_v1(channel, False, dahua_brightness)
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
        self._coordinator = coordinator

    @property
    def name(self):
        """Return the name of the light."""
        return self._coordinator.get_device_name() + " " + self._name

    @property
    def unique_id(self):
        """
        A unique identifier for this entity. Needs to be unique within a platform (ie light.hue). Should not be configurable by the user or be changeable
        see https://developers.home-assistant.io/docs/entity_registry_index/#unique-id-requirements
        """
        return self._coordinator.get_serial_number() + "_illuminator"

    @property
    def is_on(self):
        """Return true if the light is on"""
        return self._coordinator.is_illuminator_on()

    @property
    def brightness(self):
        """Return the brightness of this light between 0..255 inclusive"""

        return self._coordinator.get_illuminator_brightness()

    @property
    def supported_features(self):
        """Flag supported features."""
        return 0

    @property
    def should_poll(self):
        """Don't poll."""
        return False

    async def async_turn_on(self, **kwargs):
        """Turn the light on with the current brightness"""
        hass_brightness = kwargs.get(ATTR_BRIGHTNESS)
        dahua_brightness = dahua_utils.hass_brightness_to_dahua_brightness(hass_brightness)
        channel = self._coordinator.get_channel()
        profile_mode = self._coordinator.get_profile_mode()
        await self._coordinator.client.async_set_lighting_v2(channel, True, dahua_brightness, profile_mode)
        await self._coordinator.async_refresh()

    async def async_turn_off(self, **kwargs):
        """Turn the light off"""
        hass_brightness = kwargs.get(ATTR_BRIGHTNESS)
        dahua_brightness = dahua_utils.hass_brightness_to_dahua_brightness(hass_brightness)
        channel = self._coordinator.get_channel()
        profile_mode = self._coordinator.get_profile_mode()
        await self._coordinator.client.async_set_lighting_v2(channel, False, dahua_brightness, profile_mode)
        await self._coordinator.async_refresh()


class AmcrestRingLight(DahuaBaseEntity, LightEntity):
    """Representation of a Amcrest ring light"""

    def __init__(self, coordinator: DahuaDataUpdateCoordinator, entry, name):
        super().__init__(coordinator, entry)
        self._name = name
        self._coordinator = coordinator

    @property
    def name(self):
        """Return the name of the light."""
        return self._coordinator.get_device_name() + " " + self._name

    @property
    def unique_id(self):
        """
        A unique identifier for this entity. Needs to be unique within a platform (ie light.hue).
        Should not be configurable by the user or be changeable
        see https://developers.home-assistant.io/docs/entity_registry_index/#unique-id-requirements
        """
        return self._coordinator.get_serial_number() + "_ring_light"

    @property
    def is_on(self):
        """Return true if the light is on"""
        return self._coordinator.is_ring_light_on()

    async def async_turn_on(self, **kwargs):
        """Turn the light on"""
        await self._coordinator.client.async_set_light_global_enabled(True)
        await self._coordinator.async_refresh()

    async def async_turn_off(self, **kwargs):
        """Turn the light off"""
        await self._coordinator.client.async_set_light_global_enabled(False)
        await self._coordinator.async_refresh()


class AmcrestFloodLight(DahuaBaseEntity, LightEntity):
    """
        Representation of a Amcrest Flood Light (for cameras that have them)
        Unlike the 'Dahua Illuminator', Amcrest Flood Lights do not play nicely
        with adjusting the 'White Light' brightness.
    """

    def __init__(self, coordinator: DahuaDataUpdateCoordinator, entry, name):
        super().__init__(coordinator, entry)
        self._name = name
        self._coordinator = coordinator

    @property
    def name(self):
        """Return the name of the light."""
        return self._coordinator.get_device_name() + " " + self._name

    @property
    def unique_id(self):
        """
        A unique identifier for this entity. Needs to be unique within a platform (ie light.hue). Should not be configurable by the user or be changeable
        see https://developers.home-assistant.io/docs/entity_registry_index/#unique-id-requirements
        """
        return self._coordinator.get_serial_number() + "_flood_light"

    @property
    def is_on(self):
        """Return true if the light is on"""
        return self._coordinator.is_amcrest_flood_light_on()

    @property
    def supported_features(self):
        """Flag supported features."""
        return DAHUA_SUPPORTED_OPTIONS

    @property
    def should_poll(self):
        """Don't poll."""
        return False

    async def async_turn_on(self, **kwargs):
        """Turn the light on"""
        channel = self._coordinator.get_channel()
        profile_mode = self._coordinator.get_profile_mode()
        await self._coordinator.client.async_set_lighting_v2_for_amcrest_flood_lights(channel, True, profile_mode)
        await self._coordinator.async_refresh()

    async def async_turn_off(self, **kwargs):
        """Turn the light off"""
        channel = self._coordinator.get_channel()
        profile_mode = self._coordinator.get_profile_mode()
        await self._coordinator.client.async_set_lighting_v2_for_amcrest_flood_lights(channel, False, profile_mode)
        await self._coordinator.async_refresh()


class DahuaSecurityLight(DahuaBaseEntity, LightEntity):
    """
    Representation of a Dahua light (for cameras that have them). This is the red/blue flashing lights.
    The camera will only keep this light on for a few seconds before it automatically turns off.
    """

    def __init__(self, coordinator: DahuaDataUpdateCoordinator, entry, name):
        super().__init__(coordinator, entry)
        self._name = name
        self._coordinator = coordinator

    @property
    def name(self):
        """Return the name of the light."""
        return self._coordinator.get_device_name() + " " + self._name

    @property
    def unique_id(self):
        """
        A unique identifier for this entity. Needs to be unique within a platform (ie light.hue). Should not be configurable by the user or be changeable
        see https://developers.home-assistant.io/docs/entity_registry_index/#unique-id-requirements
        """
        return self._coordinator.get_serial_number() + "_security"

    @property
    def is_on(self):
        """Return true if the light is on"""
        return self._coordinator.is_security_light_on()

    @property
    def should_poll(self):
        """Don't poll."""
        return False

    async def async_turn_on(self, **kwargs):
        """Turn the light on"""
        channel = self._coordinator.get_channel()
        await self._coordinator.client.async_set_coaxial_control_state(channel, SECURITY_LIGHT_TYPE, True)
        await self._coordinator.async_refresh()

    async def async_turn_off(self, **kwargs):
        """Turn the light off"""
        channel = self._coordinator.get_channel()
        await self._coordinator.client.async_set_coaxial_control_state(channel, SECURITY_LIGHT_TYPE, False)
        await self._coordinator.async_refresh()

    @property
    def icon(self):
        """Return the icon of this switch."""
        return SECURITY_LIGHT_ICON
