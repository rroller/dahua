"""Binary sensor platform for dahua."""

from __future__ import annotations

import re

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from custom_components.dahua import DahuaConfigEntry, DahuaDataUpdateCoordinator

from .entity import DahuaBaseEntity

# Translation keys for known events. Events listed here will use _attr_translation_key.
# Events not listed here will compute _attr_name from the CamelCase event name.
TRANSLATION_KEY_OVERRIDES = {
    "VideoMotion": "motion_alarm",
    "CrossLineDetection": "cross_line_alarm",
    "DoorbellPressed": "button_pressed",
}

# Override the device class for events
DEVICE_CLASS_OVERRIDES = {
    "VideoMotion": BinarySensorDeviceClass.MOTION,
    "CrossLineDetection": BinarySensorDeviceClass.MOTION,
    "AlarmLocal": BinarySensorDeviceClass.SAFETY,
    "VideoLoss": BinarySensorDeviceClass.SAFETY,
    "VideoBlind": BinarySensorDeviceClass.SAFETY,
    "StorageNotExist": BinarySensorDeviceClass.CONNECTIVITY,
    "StorageFailure": BinarySensorDeviceClass.CONNECTIVITY,
    "StorageLowSpace": BinarySensorDeviceClass.SAFETY,
    "FireWarning": BinarySensorDeviceClass.SAFETY,
    "DoorbellPressed": BinarySensorDeviceClass.SOUND,
    "DoorStatus": BinarySensorDeviceClass.DOOR,
    "AudioMutation": BinarySensorDeviceClass.SOUND,
}

# Icon overrides are now in icons.json for events with translation keys.
# For dynamic events that don't have translation keys, we keep icon overrides here.
ICON_OVERRIDES = {
    "AudioAnomaly": "mdi:volume-high",
    "AudioMutation": "mdi:volume-high",
}

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DahuaConfigEntry,
    async_add_devices: AddEntitiesCallback,
) -> None:
    """Setup binary_sensor platform."""
    coordinator: DahuaDataUpdateCoordinator = entry.runtime_data

    sensors: list[DahuaEventSensor] = []
    for event_name in coordinator.get_event_list():
        sensors.append(DahuaEventSensor(coordinator, entry, event_name))

    # For doorbells we'll just add these since most people will want them
    if coordinator.is_doorbell():
        sensors.append(DahuaEventSensor(coordinator, entry, "DoorbellPressed"))
        sensors.append(DahuaEventSensor(coordinator, entry, "Invite"))
        sensors.append(DahuaEventSensor(coordinator, entry, "DoorStatus"))
        sensors.append(DahuaEventSensor(coordinator, entry, "CallNoAnswered"))

    if sensors:
        async_add_devices(sensors)


class DahuaEventSensor(DahuaBaseEntity, BinarySensorEntity):
    """
    dahua binary_sensor class to record events. Many of these events are configured in the camera UI by going to:
    Setting -> Event -> IVS -> and adding a tripwire rule, etc. Events are received via async event streaming
    in the coordinator.
    """

    def __init__(
        self,
        coordinator: DahuaDataUpdateCoordinator,
        config_entry: ConfigEntry,
        event_name: str,
    ) -> None:
        DahuaBaseEntity.__init__(self, coordinator, config_entry)
        BinarySensorEntity.__init__(self)

        # event_name is the event name, example: VideoMotion, CrossLineDetection, SmartMotionHuman, etc
        self._event_name = event_name

        self._coordinator = coordinator
        self._device_name = coordinator.get_device_name()
        self._attr_device_class = DEVICE_CLASS_OVERRIDES.get(
            event_name, BinarySensorDeviceClass.MOTION
        )

        # Use translation key for known events, compute name for dynamic/unknown events
        translation_key = TRANSLATION_KEY_OVERRIDES.get(event_name)
        if translation_key:
            self._attr_translation_key = translation_key
        else:
            # Generate friendly name from CamelCase event name
            # https://stackoverflow.com/questions/25674532/pythonic-way-to-add-space-before-capital-letter-if-and-only-if-previous-letter-i/25674575
            default_name = re.sub(r"(?<![A-Z])(?<!^)([A-Z])", r" \1", event_name)
            self._attr_name = default_name

        # Icon override for dynamic events (known events use icons.json)
        if not translation_key:
            self._icon_override = ICON_OVERRIDES.get(event_name, None)
        else:
            self._icon_override = None

        # Build the unique ID. This will convert the name to lower underscores. For example, "Smart Motion Vehicle" will
        # become "smart_motion_vehicle" and will be added as a suffix to the device serial number
        if translation_key:
            friendly_name = translation_key.replace("_", " ")
        else:
            friendly_name = self._attr_name or event_name
        self._unique_id = (
            coordinator.get_serial_number()
            + "_"
            + friendly_name.lower().replace(" ", "_")
        )
        if event_name == "VideoMotion":
            # We need this for backwards compatibility as the VideoMotion was created with a unique ID of just the
            # serial number and we don't want to break people who are upgrading
            self._unique_id = coordinator.get_serial_number()

    @property
    def unique_id(self) -> str:
        """Return the entity unique ID."""
        return self._unique_id

    @property
    def icon(self) -> str | None:
        return self._icon_override

    @property
    def is_on(self) -> bool:
        """
        Return true if the event is activated.

        This is the magic part of this sensor along with the async_added_to_hass method below.
        The async_added_to_hass method adds a listener to the coordinator so when the event is started or stopped
        it calls the schedule_update_ha_state function. schedule_update_ha_state gets the current value from this is_on method.
        """
        return self._coordinator.get_event_timestamp(self._event_name) > 0

    async def async_added_to_hass(self) -> None:
        """Connect to dispatcher listening for entity data notifications."""
        self._coordinator.add_dahua_event_listener(
            self._event_name, self.schedule_update_ha_state
        )

    @property
    def should_poll(self) -> bool:
        """Return True if entity has to be polled for state.  False if entity pushes its state to HA"""
        return False
