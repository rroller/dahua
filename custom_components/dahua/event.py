"""
Event entity platform for Dahua.
https://developers.home-assistant.io/docs/core/entity/event
"""
import logging

from homeassistant.components.event import EventDeviceClass, EventEntity
from homeassistant.core import HomeAssistant, callback

from custom_components.dahua import DahuaDataUpdateCoordinator

from .const import DOMAIN
from .entity import DahuaEventDrivenEntity

_LOGGER = logging.getLogger(__package__)

DOORBELL_PRESSED = "DoorbellPressed"

# Home Assistant requires a doorbell event entity to offer "ring", and warns
# that anything else stops working in 2027.4:
#
#     if (self.device_class == EventDeviceClass.DOORBELL
#             and DoorbellEventType.RING not in self.event_types):
#
# Spelled out rather than imported as DoorbellEventType.RING, because that
# enum is newer than the 2025.1.2 this integration supports. It is a StrEnum
# with "ring" as its only member, so the literal is the same value.
EVENT_RING = "ring"


async def async_setup_entry(hass: HomeAssistant, entry, async_add_devices):
    """Setup the event platform."""
    coordinator: DahuaDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]

    # Only a doorbell has a button to press. On anything else this entity would
    # sit at unknown for ever, which is worse than not offering it.
    if coordinator.is_doorbell():
        async_add_devices([DahuaDoorbellEvent(coordinator, entry)])


class DahuaDoorbellEvent(DahuaEventDrivenEntity, EventEntity):
    """The doorbell button, as Home Assistant's own doorbell primitive.

    #715: `event` with the `doorbell` device class is what Home Assistant
    offers for this, and what cards and integrations built on it expect. The
    Button Pressed binary sensor stays exactly as it is -- it holds a state for
    a few seconds, which is what an automation waiting on `on` needs, and this
    is momentary, which is what something showing "somebody rang at 19:42"
    needs. They are different shapes of the same fact, so both are useful.

    Both subscribe to DoorbellPressed, which is why registering a listener
    appends rather than assigns: the second one used to replace the first.
    """

    _attr_device_class = EventDeviceClass.DOORBELL
    _attr_event_types = [EVENT_RING]

    @property
    def name(self):
        return self._coordinator.get_device_name() + " Doorbell"

    @property
    def unique_id(self):
        return self._coordinator.get_serial_number() + "_doorbell_event"

    @callback
    def _async_doorbell_pressed(self) -> None:
        """Fire, but only on the press rather than on the release.

        The listener is called for the start and the end of the event, and the
        timestamp is what tells them apart -- it is set to now when the button
        goes down and zeroed when it comes up. An event entity has no concept
        of an ending, so firing on both would report two presses for one.
        """
        if self._coordinator.get_event_timestamp(DOORBELL_PRESSED) > 0:
            self._trigger_event(EVENT_RING)
            self.async_write_ha_state()

    async def async_added_to_hass(self):
        """Listen for the press."""
        self._coordinator.add_dahua_event_listener(
            DOORBELL_PRESSED, self._async_doorbell_pressed)

    @property
    def should_poll(self) -> bool:
        return False
