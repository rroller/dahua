"""
Illuminator for for Dahua cameras that have white light illuminators.

See https://developers.home-assistant.io/docs/core/entity/light
"""

import logging

from homeassistant.core import HomeAssistant
from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    LightEntity, LightEntityFeature, ColorMode,
)

from . import DahuaDataUpdateCoordinator, dahua_utils, scheme_blocking_white_light
from .const import DOMAIN, SECURITY_LIGHT_ICON, INFRARED_ICON
from .entity import DahuaBaseEntity
from .client import SECURITY_LIGHT_TYPE

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities):
    """Setup light platform."""
    coordinator = hass.data[DOMAIN][entry.entry_id]

    entities = []
    if coordinator.supports_infrared_light():
        entities.append(DahuaInfraredLight(coordinator, entry, "Infrared"))

    if coordinator.supports_illuminator():
        entities.append(DahuaIlluminator(coordinator, entry, "Illuminator"))

    if coordinator.is_flood_light():
        entities.append(FloodLight(coordinator, entry, "Flood Light"))

    has_security_light = (
        coordinator.supports_nvr_active_deterrence()
        if coordinator.is_nvr_channel()
        else coordinator.supports_security_light()
    )
    if has_security_light and not coordinator.is_amcrest_doorbell():
        #  The Amcrest doorbell works a little different and is added in select.py
        security_light_name = (
            "Warning Light" if coordinator.is_nvr_channel() else "Security Light"
        )
        entities.append(DahuaSecurityLight(coordinator, entry, security_light_name))

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
    def color_mode(self) -> ColorMode | str | None:
        """Return the color mode of the light."""
        return ColorMode.BRIGHTNESS

    @property
    def supported_color_modes(self) -> set[str]:
        """Flag supported color modes."""
        return {self.color_mode}

    @property
    def supported_features(self):
        """Flag supported features."""
        return LightEntityFeature.EFFECT

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
    def color_mode(self) -> ColorMode | str | None:
        """Return the color mode of the light."""
        return ColorMode.BRIGHTNESS
    
    @property
    def supported_color_modes(self) -> set[str]:
        """Flag supported color modes."""
        return {self.color_mode}

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
        await self._coordinator.client.async_set_lighting_v2(
            channel, True, dahua_brightness, profile_mode,
            self._coordinator.get_illuminator_index(),
            self._coordinator.get_illuminator_bank())
        await self._warn_if_the_scheme_blocks_it(channel, profile_mode)
        await self._coordinator.async_refresh()

    async def _warn_if_the_scheme_blocks_it(self, channel, profile_mode):
        """Say so when the write will not reach the light.

        Smart Dual Light cameras decide separately which emitter they are
        willing to use. While that says AIMode or InfraredMode the white light
        stays off however correct the write was, and the only symptom is an
        entity that reports on next to a light that is not. Read at command
        time, because the user can change it on the camera whenever they like.
        """
        try:
            data = await self._coordinator.client.async_get_lighting_scheme()
        except Exception:  # pylint: disable=broad-except
            # Plenty of cameras have no such table. Not being able to check is
            # not a reason to fail the command the user actually asked for.
            _LOGGER.debug("Could not read LightingScheme", exc_info=True)
            return
        blocking = scheme_blocking_white_light(data, channel, profile_mode)
        if blocking is not None:
            _LOGGER.warning(
                "The white light on %s was set, but the camera's lighting scheme is "
                "%s, so the light will not physically come on. Switch that camera to "
                "white light in its own web interface to use this entity.",
                self._coordinator.get_device_name(), blocking,
            )

    async def async_turn_off(self, **kwargs):
        """Turn the light off"""
        hass_brightness = kwargs.get(ATTR_BRIGHTNESS)
        dahua_brightness = dahua_utils.hass_brightness_to_dahua_brightness(hass_brightness)
        channel = self._coordinator.get_channel()
        profile_mode = self._coordinator.get_profile_mode()
        await self._coordinator.client.async_set_lighting_v2(
            channel, False, dahua_brightness, profile_mode,
            self._coordinator.get_illuminator_index(),
            self._coordinator.get_illuminator_bank())
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

    @property
    def color_mode(self) -> ColorMode | str | None:
        """Return the color mode of the light."""
        return ColorMode.ONOFF

    @property
    def supported_color_modes(self) -> set[str]:
        """Flag supported color modes."""
        return {self.color_mode}


class FloodLight(DahuaBaseEntity, LightEntity):
    """
        Representation of a Amcrest, Dahua, and Lorex Flood Light (for cameras that have them)
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
        return self._coordinator.is_flood_light_on()

    @property
    def supported_features(self):
        """Flag supported features."""
        return LightEntityFeature.EFFECT
    
    @property
    def color_mode(self) -> ColorMode | str | None:
        """Return the color mode of the light."""
        return ColorMode.ONOFF

    @property
    def supported_color_modes(self) -> set[str]:
        """Flag supported color modes."""
        return {self.color_mode}

    @property
    def should_poll(self):
        """Don't poll."""
        return False

    async def async_turn_on(self, **kwargs):
        """Turn the light on"""
        if self._coordinator._supports_floodlightmode:
            channel = self._coordinator.get_channel()
            self._coordinator._floodlight_mode = await self._coordinator.client.async_get_floodlightmode()
            await self._coordinator.client.async_set_floodlightmode(2)
            if self._coordinator.is_nvr_channel():
                await self._coordinator.client.async_set_nvr_coaxial_control_state(
                    self._coordinator.get_channel_number(), SECURITY_LIGHT_TYPE, True
                )
            else:
                await self._coordinator.client.async_set_coaxial_control_state(channel, SECURITY_LIGHT_TYPE, True)
            await self._coordinator.async_refresh()
        else:
            channel = self._coordinator.get_channel()
            profile_mode = self._coordinator.get_profile_mode()
            await self._coordinator.client.async_set_lighting_v2_for_flood_lights(channel, True, profile_mode)
            await self._coordinator.async_refresh()

    async def async_turn_off(self, **kwargs):
        """Turn the light off"""
        if self._coordinator._supports_floodlightmode:
            channel = self._coordinator.get_channel()
            if self._coordinator.is_nvr_channel():
                await self._coordinator.client.async_set_nvr_coaxial_control_state(
                    self._coordinator.get_channel_number(), SECURITY_LIGHT_TYPE, False
                )
            else:
                await self._coordinator.client.async_set_coaxial_control_state(channel, SECURITY_LIGHT_TYPE, False)
            await self._coordinator.client.async_set_floodlightmode(self._coordinator._floodlight_mode)
            await self._coordinator.async_refresh()
        else:
            channel = self._coordinator.get_channel()
            profile_mode = self._coordinator.get_profile_mode()
            await self._coordinator.client.async_set_lighting_v2_for_flood_lights(channel, False, profile_mode)
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
        if self._coordinator.is_nvr_channel():
            await self._coordinator.client.async_set_nvr_coaxial_control_state(
                self._coordinator.get_channel_number(), SECURITY_LIGHT_TYPE, True
            )
        else:
            await self._coordinator.client.async_set_coaxial_control_state(channel, SECURITY_LIGHT_TYPE, True)
        await self._coordinator.async_refresh()

    async def async_turn_off(self, **kwargs):
        """Turn the light off"""
        channel = self._coordinator.get_channel()
        if self._coordinator.is_nvr_channel():
            await self._coordinator.client.async_set_nvr_coaxial_control_state(
                self._coordinator.get_channel_number(), SECURITY_LIGHT_TYPE, False
            )
        else:
            await self._coordinator.client.async_set_coaxial_control_state(channel, SECURITY_LIGHT_TYPE, False)
        await self._coordinator.async_refresh()

    @property
    def icon(self):
        """Return the icon of this switch."""
        return SECURITY_LIGHT_ICON

    @property
    def color_mode(self) -> ColorMode | str | None:
        """Return the color mode of the light."""
        return ColorMode.ONOFF

    @property
    def supported_color_modes(self) -> set[str]:
        """Flag supported color modes."""
        return {self.color_mode}
