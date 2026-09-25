"""
Button entity platform for Dahua.
https://developers.home-assistant.io/docs/core/entity/button
"""
import logging

from homeassistant.components.button import ButtonDeviceClass, ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant

from custom_components.dahua import DahuaDataUpdateCoordinator

from .const import DOMAIN
from .entity import DahuaBaseEntity

_LOGGER = logging.getLogger(__package__)


async def async_setup_entry(hass: HomeAssistant, entry, async_add_devices):
    """Setup the button platform."""
    coordinator: DahuaDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]

    buttons = [DahuaRebootButton(coordinator, entry)]

    # Opening a door is a VTO operation. On anything else the endpoint is not
    # there, and a button that always errors is worse than no button.
    if coordinator.is_doorbell():
        buttons.append(DahuaOpenDoorButton(coordinator, entry))

    async_add_devices(buttons)


class DahuaRebootButton(DahuaBaseEntity, ButtonEntity):
    """Reboots the device."""

    _attr_device_class = ButtonDeviceClass.RESTART
    # Diagnostic would hide it from the device page controls; this is an action
    # the user takes deliberately, so it belongs with the configuration.
    _attr_entity_category = EntityCategory.CONFIG

    @property
    def name(self):
        return self._coordinator.get_device_name() + " Reboot"

    @property
    def unique_id(self):
        return self._coordinator.get_serial_number() + "_reboot"

    async def async_press(self) -> None:
        """Reboot the device.

        Deliberately no refresh afterwards: the device is on its way down, so
        polling it here would only turn a successful press into an error.
        """
        _LOGGER.debug("Rebooting %s", self._coordinator.get_address())
        await self._coordinator.client.reboot()


class DahuaOpenDoorButton(DahuaBaseEntity, ButtonEntity):
    """Opens the door on a VTO."""

    @property
    def name(self):
        return self._coordinator.get_device_name() + " Open Door"

    @property
    def unique_id(self):
        return self._coordinator.get_serial_number() + "_open_door"

    async def async_press(self) -> None:
        """Open the door. The VTO relocks itself on its own timer."""
        _LOGGER.debug("Opening door on %s", self._coordinator.get_address())
        await self._coordinator.client.async_access_control_open_door(1)
