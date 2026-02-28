"""
Illuminator for for Dahua cameras that have white light illuminators.

See https://developers.home-assistant.io/docs/core/entity/light
"""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.components.light import (  # type: ignore[attr-defined]
    ATTR_BRIGHTNESS,
    LightEntity,
    LightEntityFeature,
    ColorMode,
)
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import DahuaConfigEntry, DahuaDataUpdateCoordinator, dahua_utils
from .entity import DahuaBaseEntity, dahua_command
from .client import SECURITY_LIGHT_TYPE

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DahuaConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Setup light platform."""
    coordinator = entry.runtime_data

    entities: list[LightEntity] = []
    if coordinator.supports_infrared_light():
        entities.append(DahuaInfraredLight(coordinator, entry))

    if coordinator.supports_illuminator():
        entities.append(DahuaIlluminator(coordinator, entry))

    if coordinator.is_flood_light():
        entities.append(FloodLight(coordinator, entry))

    if coordinator.supports_security_light() and not coordinator.is_amcrest_doorbell():
        #  The Amcrest doorbell works a little different and is added in select.py
        entities.append(DahuaSecurityLight(coordinator, entry))

    if coordinator.is_amcrest_doorbell():
        entities.append(AmcrestRingLight(coordinator, entry))

    async_add_entities(entities)


class DahuaInfraredLight(DahuaBaseEntity, LightEntity):
    """Representation of a Dahua infrared light (for cameras that have them)"""

    _attr_translation_key = "infrared"

    def __init__(
        self, coordinator: DahuaDataUpdateCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, entry)
        self._coordinator = coordinator

    @property
    def unique_id(self) -> str:
        """
        A unique identifier for this entity. Needs to be unique within a platform (ie light.hue). Should not be configurable by the user or be changeable
        see https://developers.home-assistant.io/docs/entity_registry_index/#unique-id-requirements
        """
        return self._coordinator.get_serial_number() + "_infrared"

    @property
    def is_on(self) -> bool:
        """Return true if the light is on"""
        return self._coordinator.is_infrared_light_on()

    @property
    def brightness(self) -> int:
        """Return the brightness of this light between 0..255 inclusive"""
        return self._coordinator.get_infrared_brightness()

    @property
    def color_mode(self) -> ColorMode | None:
        """Return the color mode of the light."""
        return ColorMode.BRIGHTNESS

    @property
    def supported_color_modes(self) -> set[ColorMode]:
        """Flag supported color modes."""
        return {ColorMode.BRIGHTNESS}

    @property
    def supported_features(self) -> LightEntityFeature:
        """Flag supported features."""
        return LightEntityFeature.EFFECT

    @property
    def should_poll(self) -> bool:
        """Don't poll."""
        return False

    @dahua_command
    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the light on with the current brightness"""
        hass_brightness = kwargs.get(ATTR_BRIGHTNESS)
        dahua_brightness = dahua_utils.hass_brightness_to_dahua_brightness(
            hass_brightness
        )
        channel = self._coordinator.get_channel()
        await self._coordinator.client.async_set_lighting_v1(
            channel, True, dahua_brightness
        )
        await self.coordinator.async_refresh()

    @dahua_command
    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the light off"""
        hass_brightness = kwargs.get(ATTR_BRIGHTNESS)
        dahua_brightness = dahua_utils.hass_brightness_to_dahua_brightness(
            hass_brightness
        )
        channel = self._coordinator.get_channel()
        await self._coordinator.client.async_set_lighting_v1(
            channel, False, dahua_brightness
        )
        await self.coordinator.async_refresh()


class DahuaIlluminator(DahuaBaseEntity, LightEntity):
    """Representation of a Dahua light (for cameras that have them)"""

    _attr_translation_key = "illuminator"

    def __init__(
        self, coordinator: DahuaDataUpdateCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, entry)
        self._coordinator = coordinator

    @property
    def unique_id(self) -> str:
        """
        A unique identifier for this entity. Needs to be unique within a platform (ie light.hue). Should not be configurable by the user or be changeable
        see https://developers.home-assistant.io/docs/entity_registry_index/#unique-id-requirements
        """
        return self._coordinator.get_serial_number() + "_illuminator"

    @property
    def is_on(self) -> bool:
        """Return true if the light is on"""
        return self._coordinator.is_illuminator_on()

    @property
    def brightness(self) -> int:
        """Return the brightness of this light between 0..255 inclusive"""

        return self._coordinator.get_illuminator_brightness()

    @property
    def color_mode(self) -> ColorMode | None:
        """Return the color mode of the light."""
        return ColorMode.BRIGHTNESS

    @property
    def supported_color_modes(self) -> set[ColorMode]:
        """Flag supported color modes."""
        return {ColorMode.BRIGHTNESS}

    @property
    def should_poll(self) -> bool:
        """Don't poll."""
        return False

    @dahua_command
    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the light on with the current brightness"""
        hass_brightness = kwargs.get(ATTR_BRIGHTNESS)
        dahua_brightness = dahua_utils.hass_brightness_to_dahua_brightness(
            hass_brightness
        )
        channel = self._coordinator.get_channel()
        profile_mode = self._coordinator.get_profile_mode()
        await self._coordinator.client.async_set_lighting_v2(
            channel, True, dahua_brightness, profile_mode
        )
        await self._coordinator.async_refresh()

    @dahua_command
    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the light off"""
        hass_brightness = kwargs.get(ATTR_BRIGHTNESS)
        dahua_brightness = dahua_utils.hass_brightness_to_dahua_brightness(
            hass_brightness
        )
        channel = self._coordinator.get_channel()
        profile_mode = self._coordinator.get_profile_mode()
        await self._coordinator.client.async_set_lighting_v2(
            channel, False, dahua_brightness, profile_mode
        )
        await self._coordinator.async_refresh()


class AmcrestRingLight(DahuaBaseEntity, LightEntity):
    """Representation of a Amcrest ring light"""

    _attr_translation_key = "ring_light"

    def __init__(
        self, coordinator: DahuaDataUpdateCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, entry)
        self._coordinator = coordinator

    @property
    def unique_id(self) -> str:
        """
        A unique identifier for this entity. Needs to be unique within a platform (ie light.hue).
        Should not be configurable by the user or be changeable
        see https://developers.home-assistant.io/docs/entity_registry_index/#unique-id-requirements
        """
        return self._coordinator.get_serial_number() + "_ring_light"

    @property
    def is_on(self) -> bool:
        """Return true if the light is on"""
        return self._coordinator.is_ring_light_on()

    @dahua_command
    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the light on"""
        await self._coordinator.client.async_set_light_global_enabled(True)
        await self._coordinator.async_refresh()

    @dahua_command
    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the light off"""
        await self._coordinator.client.async_set_light_global_enabled(False)
        await self._coordinator.async_refresh()

    @property
    def color_mode(self) -> ColorMode | None:
        """Return the color mode of the light."""
        return ColorMode.ONOFF

    @property
    def supported_color_modes(self) -> set[ColorMode]:
        """Flag supported color modes."""
        return {ColorMode.ONOFF}


class FloodLight(DahuaBaseEntity, LightEntity):
    """
    Representation of a Amcrest, Dahua, and Lorex Flood Light (for cameras that have them)
    Unlike the 'Dahua Illuminator', Amcrest Flood Lights do not play nicely
    with adjusting the 'White Light' brightness.
    """

    _attr_translation_key = "flood_light"

    def __init__(
        self, coordinator: DahuaDataUpdateCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, entry)
        self._coordinator = coordinator

    @property
    def unique_id(self) -> str:
        """
        A unique identifier for this entity. Needs to be unique within a platform (ie light.hue). Should not be configurable by the user or be changeable
        see https://developers.home-assistant.io/docs/entity_registry_index/#unique-id-requirements
        """
        return self._coordinator.get_serial_number() + "_flood_light"

    @property
    def is_on(self) -> bool:
        """Return true if the light is on"""
        return self._coordinator.is_flood_light_on()

    @property
    def supported_features(self) -> LightEntityFeature:
        """Flag supported features."""
        return LightEntityFeature.EFFECT

    @property
    def color_mode(self) -> ColorMode | None:
        """Return the color mode of the light."""
        return ColorMode.ONOFF

    @property
    def supported_color_modes(self) -> set[ColorMode]:
        """Flag supported color modes."""
        return {ColorMode.ONOFF}

    @property
    def should_poll(self) -> bool:
        """Don't poll."""
        return False

    @dahua_command
    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the light on"""
        if self._coordinator._supports_floodlightmode:
            channel = self._coordinator.get_channel()
            result = await self._coordinator.client.async_get_floodlightmode()
            if isinstance(result, int):
                self._coordinator._floodlight_mode = result
            await self._coordinator.client.async_set_floodlightmode(2)
            await self._coordinator.client.async_set_coaxial_control_state(
                channel, SECURITY_LIGHT_TYPE, True
            )
            await self._coordinator.async_refresh()
        else:
            channel = self._coordinator.get_channel()
            profile_mode = self._coordinator.get_profile_mode()
            await self._coordinator.client.async_set_lighting_v2_for_flood_lights(
                channel, True, profile_mode
            )
            await self._coordinator.async_refresh()

    @dahua_command
    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the light off"""
        if self._coordinator._supports_floodlightmode:
            channel = self._coordinator.get_channel()
            await self._coordinator.client.async_set_coaxial_control_state(
                channel, SECURITY_LIGHT_TYPE, False
            )
            await self._coordinator.client.async_set_floodlightmode(
                self._coordinator._floodlight_mode
            )
            await self._coordinator.async_refresh()
        else:
            channel = self._coordinator.get_channel()
            profile_mode = self._coordinator.get_profile_mode()
            await self._coordinator.client.async_set_lighting_v2_for_flood_lights(
                channel, False, profile_mode
            )
            await self._coordinator.async_refresh()


class DahuaSecurityLight(DahuaBaseEntity, LightEntity):
    """
    Representation of a Dahua light (for cameras that have them). This is the red/blue flashing lights.
    The camera will only keep this light on for a few seconds before it automatically turns off.
    """

    _attr_translation_key = "security_light"

    def __init__(
        self, coordinator: DahuaDataUpdateCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, entry)
        self._coordinator = coordinator

    @property
    def unique_id(self) -> str:
        """
        A unique identifier for this entity. Needs to be unique within a platform (ie light.hue). Should not be configurable by the user or be changeable
        see https://developers.home-assistant.io/docs/entity_registry_index/#unique-id-requirements
        """
        return self._coordinator.get_serial_number() + "_security"

    @property
    def is_on(self) -> bool:
        """Return true if the light is on"""
        return self._coordinator.is_security_light_on()

    @property
    def should_poll(self) -> bool:
        """Don't poll."""
        return False

    @dahua_command
    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the light on"""
        channel = self._coordinator.get_channel()
        await self._coordinator.client.async_set_coaxial_control_state(
            channel, SECURITY_LIGHT_TYPE, True
        )
        await self._coordinator.async_refresh()

    @dahua_command
    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the light off"""
        channel = self._coordinator.get_channel()
        await self._coordinator.client.async_set_coaxial_control_state(
            channel, SECURITY_LIGHT_TYPE, False
        )
        await self._coordinator.async_refresh()

    @property
    def color_mode(self) -> ColorMode | None:
        """Return the color mode of the light."""
        return ColorMode.ONOFF

    @property
    def supported_color_modes(self) -> set[ColorMode]:
        """Flag supported color modes."""
        return {ColorMode.ONOFF}
