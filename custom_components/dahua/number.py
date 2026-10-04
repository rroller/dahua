"""Number entity platform for Dahua: picture adjustments.

Brightness, contrast, saturation and hue, the controls Reolink and Tapo expose
as sliders and this integration did not. Backed by the VideoColor config table
(measured 0-100 on a DHI-NVR5464 and a VTO), read on the poll and written per
change to the channel's general profile (index 0).
"""

import logging

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant

from custom_components.dahua import DahuaDataUpdateCoordinator, entry_coordinators

from .const import DOMAIN
from .entity import DahuaBaseEntity

_LOGGER = logging.getLogger(__package__)

# Writes move the device, so one at a time, like the other acting platforms.
PARALLEL_UPDATES = 1

# entity key -> (display name, VideoColor field). The key is the unique-id
# suffix; the field is what the device stores.
IMAGE_ADJUSTMENTS = (
    ("image_brightness", "Brightness", "Brightness"),
    ("image_contrast", "Contrast", "Contrast"),
    ("image_saturation", "Saturation", "Saturation"),
    ("image_hue", "Hue", "Hue"),
)


async def async_setup_entry(hass: HomeAssistant, entry, async_add_devices):
    """Set up the number platform."""
    for coordinator in entry_coordinators(entry).values():
        # An indoor monitor without a camera has no picture to adjust.
        if coordinator.is_indoor_monitor_without_video():
            continue
        numbers = [
            DahuaImageAdjustmentNumber(coordinator, entry, key, name, field)
            for key, name, field in IMAGE_ADJUSTMENTS
        ]
        if numbers:
            async_add_devices(numbers, config_subentry_id=coordinator.subentry_id)


class DahuaImageAdjustmentNumber(DahuaBaseEntity, NumberEntity):
    """One picture adjustment (0-100) on a channel's general VideoColor profile."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_mode = NumberMode.SLIDER

    def __init__(self, coordinator, entry, key, name, field):
        DahuaBaseEntity.__init__(self, coordinator, entry)
        NumberEntity.__init__(self)
        self._coordinator = coordinator
        self._key = key
        self._field = field
        self._attr_name = name
        self._attr_unique_id = "%s_%s" % (coordinator.get_serial_number(), key)

    @property
    def available(self) -> bool:
        """Off until the poll has a value, so a device without VideoColor does not
        show four sliders stuck at an invented default."""
        return (
            super().available
            and self._coordinator.get_video_color(self._field) is not None
        )

    @property
    def native_value(self):
        return self._coordinator.get_video_color(self._field)

    async def async_set_native_value(self, value: float) -> None:
        await self._coordinator.client.async_set_video_color(
            self._coordinator.get_channel(), self._field, int(value)
        )
        await self._coordinator.async_refresh()
