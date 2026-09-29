"""
Button entity platform for Dahua.
https://developers.home-assistant.io/docs/core/entity/button
"""
import logging

from homeassistant.components.button import ButtonDeviceClass, ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant

from homeassistant.exceptions import HomeAssistantError

from custom_components.dahua import DahuaDataUpdateCoordinator, entry_coordinators
from custom_components.dahua.vto import CancelCallRefused

from .const import DOMAIN
from .entity import DahuaBaseEntity

_LOGGER = logging.getLogger(__package__)


# One at a time, because reboot and door open are writes and these devices are measurably intolerant of
# concurrent requests: MAX_CONCURRENT_REQUESTS_PER_HOST is 2 for the same reason,
# and the login storms behind #577 and #603 are what happens without it. A
# coordinator does not help here, since it only centralises inbound reads and
# leaves outbound actions uncontrolled.
PARALLEL_UPDATES = 1

async def async_setup_entry(hass: HomeAssistant, entry, async_add_devices):
    """Setup the button platform."""
    for coordinator in entry_coordinators(entry).values():
        buttons = [DahuaRebootButton(coordinator, entry)]

        # Opening a door is a VTO operation. On anything else the endpoint is not
        # there, and a button that always errors is worse than no button.
        if coordinator.is_doorbell():
            buttons.append(DahuaOpenDoorButton(coordinator, entry))
            buttons.append(DahuaCancelCallButton(coordinator, entry))

        async_add_devices(buttons)


class DahuaRebootButton(DahuaBaseEntity, ButtonEntity):
    """Reboots the device."""

    _attr_translation_key = "reboot"

    _attr_device_class = ButtonDeviceClass.RESTART
    # Diagnostic would hide it from the device page controls; this is an action
    # the user takes deliberately, so it belongs with the configuration.
    _attr_entity_category = EntityCategory.CONFIG

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

    _attr_translation_key = "open_door"

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

    _attr_translation_key = "cancel_call"

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
