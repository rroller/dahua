"""
Event entity platform for Dahua.
https://developers.home-assistant.io/docs/core/entity/event
"""

import logging

from homeassistant.components.event import EventDeviceClass, EventEntity
from homeassistant.core import HomeAssistant, callback

from custom_components.dahua import entry_coordinators

from .binary_sensor import MOMENTARY_EVENT_HOLD_SECONDS
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
# enum is newer than the oldest Home Assistant this integration supports
# (2026.8.0, per hacs.json). It is a StrEnum
# with "ring" as its only member, so the literal is the same value.
EVENT_RING = "ring"

# How long after a ring another ringing state is still the same press.
#
# One press is not one event. A DHI-VTO2202F-P-S2 reports a single button press
# as two BackKeyLight pulses 96 ms apart, State 1 and then State 2 (#944), and
# both are in DOORBELL_RINGING_STATES because some devices send only the one and
# some only the other. So the listener runs twice for one caller, and an entity
# that fires on each run reports two rings.
#
# The Button Pressed binary sensor never showed this, which is why the report
# came in as "the event entity disagrees with the binary sensor". That sensor is
# level shaped: the second state re-stamps a sensor that is already on and
# nothing changes. This one is edge shaped, so the edge has to be found here.
#
# Borrowed from the binary sensor rather than written as another 5, so that "the
# same press" means one thing in the integration. While that sensor still shows
# the press, this entity treats a further ringing state as the same press.
SAME_PRESS_SECONDS = MOMENTARY_EVENT_HOLD_SECONDS["DoorbellPressed"]


# A coordinator centralises the inbound reads, and nothing here sends a command,
# so there is nothing to serialise: read only: fired by the event stream, never polled.
PARALLEL_UPDATES = 0


async def async_setup_entry(hass: HomeAssistant, entry, async_add_devices):
    """Setup the event platform."""
    for coordinator in entry_coordinators(entry).values():
        # Only a doorbell has a button to press. On anything else this entity would
        # sit at unknown for ever, which is worse than not offering it.
        if coordinator.is_doorbell():
            async_add_devices(
                [DahuaDoorbellEvent(coordinator, entry)],
                config_subentry_id=coordinator.subentry_id,
            )


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
    _attr_translation_key = "doorbell"

    # The coordinator timestamp this entity last rang on, or None if it is not
    # currently in a ring. A class attribute because much of the suite builds
    # entities with `object.__new__` and never runs `__init__`.
    _rang_at = None

    @property
    def unique_id(self):
        return self._coordinator.get_serial_number() + "_doorbell_event"

    @callback
    def _async_doorbell_pressed(self) -> None:
        """Fire once per press, on the press rather than on the release.

        The listener is called for every call state the doorbell reports, and
        the timestamp is what tells a press from a release -- it is set to now
        while the button is down and zeroed when the call stops ringing. An
        event entity has no concept of an ending, so firing on both would
        report two presses for one.

        A ring also has to be told from the same ring reported again, which the
        timestamp alone does not do: a device that sends State 1 and State 2 for
        one press stamps it twice, and both are presses by that test. So the
        timestamp rung on is kept, and another ring within SAME_PRESS_SECONDS of
        it is the same caller.

        Re-armed by the release, and by the window expiring. Either alone would
        be a bug: a device that never reports a non-ringing state would never
        ring again on the first, and the second alone would let the two states
        of one press through whenever they straddled the window.
        """
        timestamp = self._coordinator.get_event_timestamp(DOORBELL_PRESSED)
        if timestamp <= 0:
            self._rang_at = None
            return
        if self._rang_at is not None and timestamp - self._rang_at < SAME_PRESS_SECONDS:
            return
        self._rang_at = timestamp
        self._trigger_event(EVENT_RING)
        self.async_write_ha_state()

    async def async_added_to_hass(self):
        """Listen for the press, and stop listening when removed."""
        self.async_on_remove(
            self._coordinator.add_dahua_event_listener(
                DOORBELL_PRESSED, self._async_doorbell_pressed
            )
        )

    @property
    def should_poll(self) -> bool:
        return False
