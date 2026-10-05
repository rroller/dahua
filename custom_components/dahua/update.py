"""Firmware update entity.

Reports the firmware the device itself says is available against the firmware
it is running. The device learns of a newer image from its own cloud OTA check;
Home Assistant only reads the record that check leaves behind in the
``_DHCloudUpgrade_`` config table, so this entity causes no cloud traffic and
no Dahua login of its own.

Informational only -- it exposes no install feature. A failed or interrupted
flash bricks the camera, and the images are model and OEM specific, so
installing stays a deliberate act on the camera's own web UI or app. The entity
answers one question: is there a newer firmware than the one running?
"""

from homeassistant.components.update import UpdateEntity, UpdateEntityFeature
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant

from custom_components.dahua import entry_coordinators

from .dahua_utils import clean_firmware_version, firmware_is_newer
from .entity import DahuaBaseEntity

# Nothing here sends a command; every value comes from the coordinator.
PARALLEL_UPDATES = 0


async def async_setup_entry(hass: HomeAssistant, entry, async_add_devices):
    """Setup the update platform."""
    for coordinator in entry_coordinators(entry).values():
        # No curated table and no separate cloud call: the entity exists only
        # where the device answered the cloud OTA record read at setup. On a
        # device that did not, an update entity could only ever read "unknown",
        # which is worse than no entity at all (the profile sensor's rule).
        if not coordinator.supports_cloud_upgrade():
            continue

        async_add_devices(
            [DahuaFirmwareUpdateEntity(coordinator, entry)],
            config_subentry_id=coordinator.subentry_id,
        )


class DahuaFirmwareUpdateEntity(DahuaBaseEntity, UpdateEntity):
    """The running firmware against the newest the device knows of."""

    _attr_translation_key = "firmware_update"

    # Informational, not a control: it belongs under Diagnostics, like the
    # firmware version sensor. (The base would infer this from the missing
    # INSTALL feature; stated because it is a decision, not a side effect.)
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    # No install, no backup, no specific version: this reports and stops.
    _attr_supported_features = UpdateEntityFeature(0)

    @property
    def unique_id(self):
        return self._coordinator.get_serial_number() + "_firmware_update"

    @property
    def installed_version(self) -> str | None:
        # The reported string carries its build date ("...,build:2020-06-05");
        # the comparable version is the part before it.
        version = clean_firmware_version(self._coordinator.get_firmware_version())
        return version or None

    @property
    def latest_version(self) -> str | None:
        return self._coordinator.get_cloud_firmware_version()

    def version_is_newer(self, latest_version: str, installed_version: str) -> bool:
        """Order Dahua firmware strings the way Home Assistant cannot.

        The base class compares with AwesomeVersion, which calls
        ``2.800.0000016.0.R`` unknown and raises; the update component reads a
        raised comparison as "an update is available", so leaving it alone
        would flag every up-to-date camera. Comparing the numeric components
        avoids both the exception and that false alarm.
        """
        return firmware_is_newer(latest_version, installed_version)
