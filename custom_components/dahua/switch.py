"""Switch platform for dahua."""

from __future__ import annotations

from typing import Any

from aiohttp import ClientError
from homeassistant.core import HomeAssistant
from homeassistant.components.switch import SwitchEntity
from homeassistant.helpers.entity import EntityCategory  # type: ignore[attr-defined]
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from custom_components.dahua import DahuaConfigEntry, DahuaDataUpdateCoordinator

from .entity import DahuaBaseEntity, dahua_command
from .client import SIREN_TYPE

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DahuaConfigEntry,
    async_add_devices: AddEntitiesCallback,
) -> None:
    """Setup sensor platform."""
    coordinator: DahuaDataUpdateCoordinator = entry.runtime_data

    # I think most cameras have a motion sensor so we'll blindly add a switch for it
    devices: list[SwitchEntity] = [
        DahuaMotionDetectionBinarySwitch(coordinator, entry),
    ]

    # But only some cams have a siren, very few do actually
    if coordinator.supports_siren():
        devices.append(DahuaSirenBinarySwitch(coordinator, entry))
    if (
        coordinator.supports_smart_motion_detection()
        or coordinator.supports_smart_motion_detection_amcrest()
    ):
        devices.append(DahuaSmartMotionDetectionBinarySwitch(coordinator, entry))

    try:
        await coordinator.client.async_get_disarming_linkage()
        devices.append(DahuaDisarmingLinkageBinarySwitch(coordinator, entry))
        devices.append(
            DahuaDisarmingEventNotificationsLinkageBinarySwitch(coordinator, entry)
        )
    except ClientError:
        pass

    async_add_devices(devices)


class DahuaMotionDetectionBinarySwitch(DahuaBaseEntity, SwitchEntity):
    """dahua motion detection switch class. Used to enable or disable motion detection"""

    _attr_translation_key = "motion_detection"
    _attr_entity_category = EntityCategory.CONFIG

    @dahua_command
    async def async_turn_on(self, **kwargs: Any) -> None:  # pylint: disable=unused-argument
        """Turn on/enable motion detection."""
        channel = self._coordinator.get_channel()
        await self._coordinator.client.enable_motion_detection(channel, True)
        await self._coordinator.async_refresh()

    @dahua_command
    async def async_turn_off(self, **kwargs: Any) -> None:  # pylint: disable=unused-argument
        """Turn off/disable motion detection."""
        channel = self._coordinator.get_channel()
        await self._coordinator.client.enable_motion_detection(channel, False)
        await self._coordinator.async_refresh()

    @property
    def unique_id(self) -> str:
        """
        A unique identifier for this entity. Needs to be unique within a platform (ie light.hue). Should not be configurable by the user or be changeable
        see https://developers.home-assistant.io/docs/entity_registry_index/#unique-id-requirements
        """
        return self._coordinator.get_serial_number() + "_motion_detection"

    @property
    def is_on(self) -> bool:
        """
        Return true if the switch is on.
        Value is fetched from api.get_motion_detection_config
        """
        return self._coordinator.is_motion_detection_enabled()


class DahuaDisarmingLinkageBinarySwitch(DahuaBaseEntity, SwitchEntity):
    """will set the camera's disarming linkage (Event -> Disarming in the UI)"""

    _attr_translation_key = "disarming"
    _attr_entity_category = EntityCategory.CONFIG

    @dahua_command
    async def async_turn_on(self, **kwargs: Any) -> None:  # pylint: disable=unused-argument
        """Turn on/enable linkage"""
        channel = self._coordinator.get_channel()
        await self._coordinator.client.async_set_disarming_linkage(channel, True)
        await self._coordinator.async_refresh()

    @dahua_command
    async def async_turn_off(self, **kwargs: Any) -> None:  # pylint: disable=unused-argument
        """Turn off/disable linkage"""
        channel = self._coordinator.get_channel()
        await self._coordinator.client.async_set_disarming_linkage(channel, False)
        await self._coordinator.async_refresh()

    @property
    def unique_id(self) -> str:
        """
        A unique identifier for this entity. Needs to be unique within a platform (ie light.hue). Should not be
        configurable by the user or be changeable see
        https://developers.home-assistant.io/docs/entity_registry_index/#unique-id-requirements
        """
        return self._coordinator.get_serial_number() + "_disarming"

    @property
    def is_on(self) -> bool:
        """
        Return true if the switch is on.
        Value is fetched from client.async_get_linkage
        """
        return self._coordinator.is_disarming_linkage_enabled()


class DahuaDisarmingEventNotificationsLinkageBinarySwitch(
    DahuaBaseEntity, SwitchEntity
):
    """will set the camera's event notifications when device is disarmed (Event -> Disarming -> Event Notifications in the UI)"""

    _attr_translation_key = "event_notifications"
    _attr_entity_category = EntityCategory.CONFIG

    @dahua_command
    async def async_turn_on(self, **kwargs: Any) -> None:  # pylint: disable=unused-argument
        """Turn on/enable event notifications"""
        channel = self._coordinator.get_channel()
        await self._coordinator.client.async_set_event_notifications(channel, True)
        await self._coordinator.async_refresh()

    @dahua_command
    async def async_turn_off(self, **kwargs: Any) -> None:  # pylint: disable=unused-argument
        """Turn off/disable event notifications"""
        channel = self._coordinator.get_channel()
        await self._coordinator.client.async_set_event_notifications(channel, False)
        await self._coordinator.async_refresh()

    @property
    def unique_id(self) -> str:
        """
        A unique identifier for this entity. Needs to be unique within a platform (ie light.hue). Should not be
        configurable by the user or be changeable see
        https://developers.home-assistant.io/docs/entity_registry_index/#unique-id-requirements
        """
        return self._coordinator.get_serial_number() + "_event_notifications"

    @property
    def is_on(self) -> bool:
        """
        Return true if the switch is on.
        """
        return self._coordinator.is_event_notifications_enabled()


class DahuaSmartMotionDetectionBinarySwitch(DahuaBaseEntity, SwitchEntity):
    """Enables or disables the Smart Motion Detection option in the camera"""

    _attr_translation_key = "smart_motion_detection"
    _attr_entity_category = EntityCategory.CONFIG

    @dahua_command
    async def async_turn_on(self, **kwargs: Any) -> None:  # pylint: disable=unused-argument
        """Turn on SmartMotionDetect"""
        if self._coordinator.supports_smart_motion_detection_amcrest():
            await self._coordinator.client.async_set_ivs_rule(0, 0, True)
        else:
            await self._coordinator.client.async_enabled_smart_motion_detection(True)
        await self._coordinator.async_refresh()

    @dahua_command
    async def async_turn_off(self, **kwargs: Any) -> None:  # pylint: disable=unused-argument
        """Turn off SmartMotionDetect"""
        if self._coordinator.supports_smart_motion_detection_amcrest():
            await self._coordinator.client.async_set_ivs_rule(0, 0, False)
        else:
            await self._coordinator.client.async_enabled_smart_motion_detection(False)
        await self._coordinator.async_refresh()

    @property
    def unique_id(self) -> str:
        """
        A unique identifier for this entity. Needs to be unique within a platform (ie light.hue). Should not be
        configurable by the user or be changeable see
        https://developers.home-assistant.io/docs/entity_registry_index/#unique-id-requirements
        """
        return self._coordinator.get_serial_number() + "_smart_motion_detection"

    @property
    def is_on(self) -> bool:
        """Return true if the switch is on."""
        return self._coordinator.is_smart_motion_detection_enabled()


class DahuaSirenBinarySwitch(DahuaBaseEntity, SwitchEntity):
    """dahua siren switch class. Used to enable or disable camera built in sirens"""

    _attr_translation_key = "siren"

    @dahua_command
    async def async_turn_on(self, **kwargs: Any) -> None:  # pylint: disable=unused-argument
        """Turn on/enable the camera's siren"""
        channel = self._coordinator.get_channel()
        await self._coordinator.client.async_set_coaxial_control_state(
            channel, SIREN_TYPE, True
        )
        await self._coordinator.async_refresh()

    @dahua_command
    async def async_turn_off(self, **kwargs: Any) -> None:  # pylint: disable=unused-argument
        """Turn off/disable camera siren"""
        channel = self._coordinator.get_channel()
        await self._coordinator.client.async_set_coaxial_control_state(
            channel, SIREN_TYPE, False
        )
        await self._coordinator.async_refresh()

    @property
    def unique_id(self) -> str:
        """
        A unique identifier for this entity. Needs to be unique within a platform (ie light.hue). Should not be configurable by the user or be changeable
        see https://developers.home-assistant.io/docs/entity_registry_index/#unique-id-requirements
        """
        return self._coordinator.get_serial_number() + "_siren"

    @property
    def is_on(self) -> bool:
        """
        Return true if the siren is on.
        Value is fetched from api.get_motion_detection_config
        """
        return self._coordinator.is_siren_on()
