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


async def async_setup_entry(hass: HomeAssistant, entry, async_add_devices):
    """Setup the sensor platform."""
    coordinator: DahuaDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_devices([
        DahuaFirmwareVersionSensor(coordinator, entry),
        DahuaSerialNumberSensor(coordinator, entry),
    ])


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
