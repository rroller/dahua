"""Text entity platform for Dahua: the two OSD titles.

Both titles already have write-only services (`dahua.set_channel_title`,
`dahua.set_text_overlay`) and read-back services (`dahua.get_channel_title`,
`dahua.get_overlay_text`). These entities are the editable field for the same
two values, gated on the table the camera reports:

    ChannelTitle[ch].Name                 the channel title
    VideoWidget[ch].CustomTitle[1].Text   the custom text overlay, group 1

Group 1 is the group `set_text_overlay` and `get_overlay_text` default to and
the one the Custom Title Overlay switch controls, so the field and the switch
move the same overlay.
"""

from homeassistant.components.text import TextEntity
from homeassistant.const import EntityCategory

from . import DahuaDataUpdateCoordinator, entry_coordinators
from .entity import DahuaBaseEntity
from .late import async_add_and_keep_watching

# Every field is a write to the device, and these devices are measurably
# intolerant of concurrent requests.
PARALLEL_UPDATES = 1


async def async_setup_entry(hass, entry, async_add_devices):
    """Set up the title text fields this channel reported."""

    async def build(coordinator):
        """Which titles this channel warrants, as the device answers today.

        Called again when a probe that timed out at setup answers on a later
        poll, so a field that arrived late is created then rather than at the
        next reload (#1057).
        """
        devices = []
        if coordinator.supports_channel_title():
            devices.append(DahuaChannelTitleText(coordinator, entry))
        if coordinator.supports_custom_title():
            devices.append(DahuaCustomTitleText(coordinator, entry))
        return devices

    await async_add_and_keep_watching(
        hass,
        entry,
        build,
        async_add_devices,
        entry_coordinators,
    )


class DahuaTitleText(DahuaBaseEntity, TextEntity):
    """Shared behaviour for one title field.

    The subclasses differ only in the value they read and write, so the name
    comes from each subclass's translation key and the coordinator owns the
    read and the write.
    """

    _attr_entity_category = EntityCategory.CONFIG
    _attr_native_min = 0

    def __init__(self, coordinator: DahuaDataUpdateCoordinator, config_entry):
        super().__init__(coordinator, config_entry)
        self._coordinator = coordinator

    @property
    def unique_id(self):
        return "{0}_{1}".format(
            self._coordinator.get_serial_number(), self._attr_translation_key
        )

    @property
    def available(self) -> bool:
        """Off until the poll has a value, so a failed read is not a blank title."""
        return super().available and self.native_value is not None


class DahuaChannelTitleText(DahuaTitleText):
    """The channel name shown in the OSD (ChannelTitle[ch].Name)."""

    _attr_translation_key = "channel_title"
    _attr_native_max = 64

    @property
    def native_value(self) -> str | None:
        return self._coordinator.get_channel_title()

    async def async_set_value(self, value: str) -> None:
        await self._coordinator.async_set_channel_title(value)
        await self._coordinator.async_refresh()


class DahuaCustomTitleText(DahuaTitleText):
    """The custom text overlay, group 1 (VideoWidget[ch].CustomTitle[1].Text)."""

    _attr_translation_key = "custom_title"
    _attr_native_max = 128

    @property
    def native_value(self) -> str | None:
        return self._coordinator.get_custom_title()

    async def async_set_value(self, value: str) -> None:
        await self._coordinator.async_set_custom_title(value)
        await self._coordinator.async_refresh()
