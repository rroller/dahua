"""Diagnostic sensors for Dahua devices.

Both of these report values the coordinator already holds from setup, so the
platform adds no requests at all. Anything that would need its own API call --
storage use is the obvious one -- deliberately does not live here, because a
diagnostic is not worth another login on a device that logs every one.
"""

from homeassistant.components.sensor import SensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant

from custom_components.dahua import DahuaDataUpdateCoordinator

from .const import DOMAIN
from .entity import DahuaBaseEntity

# Maps the camera's lighting profile id to a readable label.
PROFILE_NAMES = {
    "0": "Day",
    "1": "Night",
    "2": "Scene",
}


async def async_setup_entry(hass: HomeAssistant, entry, async_add_devices):
    """Setup the sensor platform."""
    coordinator: DahuaDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]

    sensors = [
        DahuaFirmwareVersionSensor(coordinator, entry),
        DahuaSerialNumberSensor(coordinator, entry),
    ]

    # The profile is only ever read for devices that answered the Lighting
    # probe. Adding the sensor unconditionally would show "Day" forever on a
    # doorbell or a camera without selectable profiles, which is worse than
    # no sensor at all (see #641 review).
    if coordinator.supports_profile_mode():
        sensors.append(DahuaProfileSensor(coordinator, entry))

    async_add_devices(sensors)


class DahuaFirmwareVersionSensor(DahuaBaseEntity, SensorEntity):
    """The firmware the device reports.

    It is already in the device registry, but only as a label. As a sensor it
    can be templated and compared, which is what makes "tell me when a camera
    is behind" possible.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def name(self):
        return self._coordinator.get_device_name() + " Firmware Version"

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

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def name(self):
        return self._coordinator.get_device_name() + " Serial Number"

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

    _attr_icon = "mdi:theme-light-dark"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def name(self):
        return self._coordinator.get_device_name() + " Profile"

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
