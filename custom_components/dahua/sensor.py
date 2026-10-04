"""Diagnostic sensors for Dahua devices.

Both of these report values the coordinator already holds from setup, so the
platform adds no requests at all. Anything that would need its own API call --
storage use is the obvious one -- deliberately does not live here, because a
diagnostic is not worth another login on a device that logs every one.
"""

from homeassistant.components.sensor import SensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant

from custom_components.dahua import DahuaDataUpdateCoordinator, entry_coordinators

from .const import DOMAIN
from .entity import DahuaBaseEntity, DahuaEventDrivenEntity

# Maps the camera's lighting profile id to a readable label.
PROFILE_NAMES = {
    "0": "Day",
    "1": "Night",
    "2": "Scene",
}


# A coordinator centralises the inbound reads, and nothing here sends a command,
# so there is nothing to serialise: read only: every state comes from the coordinator.
PARALLEL_UPDATES = 0


async def async_setup_entry(hass: HomeAssistant, entry, async_add_devices):
    """Setup the sensor platform."""
    for coordinator in entry_coordinators(entry).values():
        sensors = [
            DahuaFirmwareVersionSensor(coordinator, entry),
            DahuaSerialNumberSensor(coordinator, entry),
        ]
        # A plate is read from a picture, which an indoor monitor without a
        # camera does not have.
        if not coordinator.is_indoor_monitor_without_video():
            sensors.append(DahuaLicensePlateSensor(coordinator, entry))

        # The profile is only ever read for devices that answered the Lighting
        # probe. Adding the sensor unconditionally would show "Day" forever on a
        # doorbell or a camera without selectable profiles, which is worse than
        # no sensor at all (see #641 review).
        if coordinator.supports_profile_mode():
            sensors.append(DahuaProfileSensor(coordinator, entry))

        async_add_devices(sensors, config_subentry_id=coordinator.subentry_id)


class DahuaFirmwareVersionSensor(DahuaBaseEntity, SensorEntity):
    """The firmware the device reports.

    It is already in the device registry, but only as a label. As a sensor it
    can be templated and compared, which is what makes "tell me when a camera
    is behind" possible.
    """

    _attr_translation_key = "firmware_version"

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def unique_id(self):
        return self._coordinator.get_serial_number() + "_firmware_version"

    @property
    def native_value(self):
        return self._coordinator.get_firmware_version()

    @property
    def extra_state_attributes(self):
        """Expose the build date when the camera reports one."""
        attrs = super().extra_state_attributes
        build_date = self._coordinator.get_build_date()
        if build_date:
            attrs["build_date"] = build_date
        return attrs


class DahuaSerialNumberSensor(DahuaBaseEntity, SensorEntity):
    """The serial the device reports."""

    _attr_translation_key = "serial_number"

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def unique_id(self):
        return self._coordinator.get_serial_number() + "_serial_number"

    @property
    def native_value(self):
        # The device's own serial, not the channel-suffixed entity key: every
        # channel of one NVR is the same physical box and should say so.
        return self._coordinator.get_device_serial_number()


class DahuaProfileSensor(DahuaBaseEntity, SensorEntity):
    """Sensor for the day/night lighting profile the camera is using right now."""

    _attr_translation_key = "profile"

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def unique_id(self):
        return self._coordinator.get_serial_number() + "_profile"

    @property
    def native_value(self) -> str:
        mode = str(self._coordinator.get_profile_mode())
        return PROFILE_NAMES.get(mode, str(mode))

    @property
    def extra_state_attributes(self):
        """Expose the raw profile number (0=day, 1=night, 2=scene)."""
        attrs = super().extra_state_attributes
        attrs["profile_number"] = self._coordinator.get_profile_mode()
        return attrs


class DahuaLicensePlateSensor(DahuaEventDrivenEntity, SensorEntity):
    """The last recognized license plate reported by the camera."""

    _attr_translation_key = "license_plate"

    def __init__(self, coordinator: DahuaDataUpdateCoordinator, entry):
        super().__init__(coordinator, entry)
        self._unique_id = f"{coordinator.get_serial_number()}_license_plate"

    @property
    def unique_id(self):
        return self._unique_id

    @property
    def native_value(self):
        val = self._coordinator.get_last_plate()
        return val if val != "unknown" else None

    @property
    def extra_state_attributes(self):
        """The plate's own fields, on top of what every entity here reports.

        Returned alone until now, so `id` and `integration` were missing from this
        sensor and only this one. Merged rather than replaced, and `or {}` because
        get_last_plate_data returns None before the first plate.
        """
        return {
            **(super().extra_state_attributes or {}),
            **(self._coordinator.get_last_plate_data() or {}),
        }

    async def async_added_to_hass(self):
        """Listen for a plate, and stop listening when removed.

        The remover has to be kept. add_plate_listener returns it for exactly this
        reason, and the authorized vehicle sensor next door uses it; this caller was
        missed. Without it the callback outlives the entity, so every reload leaves
        another dead one in the list and each ANPR plate then logs "Error calling plate
        listener" once per reload the entry has ever had.
        """
        self.async_on_remove(
            self._coordinator.add_plate_listener(self.schedule_update_ha_state)
        )

    @property
    def should_poll(self) -> bool:
        return False
