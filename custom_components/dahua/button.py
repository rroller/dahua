"""
Button entity platform for Dahua.
https://developers.home-assistant.io/docs/core/entity/button
"""
import logging

from homeassistant.components.button import ButtonDeviceClass, ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant

from homeassistant.exceptions import HomeAssistantError

from custom_components.dahua import DahuaDataUpdateCoordinator
from custom_components.dahua.vto import CancelCallRefused

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
        buttons.append(DahuaCancelCallButton(coordinator, entry))

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


class DahuaCancelCallButton(DahuaBaseEntity, ButtonEntity):
    """Hangs up a call the doorbell is making.

    #716. The same thing the vto_cancel_call service does, without needing a
    camera entity kept alive purely to have something to call it on.

    Left out of #552 originally because cancel_call returned True without
    waiting for the doorbell, so this would have gone green every time. It
    waits now, which is what makes a button honest.
    """

    @property
    def name(self):
        return self._coordinator.get_device_name() + " Cancel Call"

    @property
    def unique_id(self):
        return self._coordinator.get_serial_number() + "_cancel_call"

    async def async_press(self) -> None:
        """Hang up. Raises if the doorbell does not agree."""
        vto_client = self._coordinator.get_vto_client()
        if vto_client is None:
            raise HomeAssistantError(
                "{0} has no doorbell connection to cancel a call on. It comes back when the event connection reconnects.".format(
                    self._coordinator.get_device_name()))
        _LOGGER.debug("Cancelling call on %s", self._coordinator.get_address())
        try:
            await vto_client.cancel_call()
        except CancelCallRefused as refused:
            raise HomeAssistantError(str(refused)) from refused
