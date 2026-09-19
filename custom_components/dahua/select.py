"""Select entity platform for Dahua."""
import logging

from homeassistant.core import HomeAssistant
from homeassistant.components.select import SelectEntity
from custom_components.dahua import DahuaDataUpdateCoordinator

from .const import DOMAIN
from .entity import DahuaBaseEntity
from .model_profiles import is_sdt4e425

_LOGGER = logging.getLogger(__package__)


async def async_setup_entry(hass: HomeAssistant, entry, async_add_devices):
    """Setup select platform."""
    coordinator: DahuaDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    devices = []

    if coordinator.is_amcrest_doorbell() and coordinator.supports_security_light():
        devices.append(DahuaDoorbellLightSelect(coordinator, entry))

    if is_sdt4e425(coordinator.get_model()):
        try:
            preset_ids = await coordinator.client.async_get_ptz_preset_ids(1)
        except Exception:
            _LOGGER.warning(
                "Unable to enumerate SDT4E425 presets through RPC2", exc_info=True
            )
            preset_ids = []
        devices.append(
            DahuaCameraPresetPositionSelect(
                coordinator, entry, preset_ids=preset_ids, rpc2_channel=1
            )
        )
    else:
        devices.append(DahuaCameraPresetPositionSelect(coordinator, entry))

    if coordinator.supports_day_night_color():
        devices.append(DahuaDayNightModeSelect(coordinator, entry))

    async_add_devices(devices)


class DahuaDoorbellLightSelect(DahuaBaseEntity, SelectEntity):
    """Allow one to turn the doorbell light on/off/strobe."""

    def __init__(self, coordinator: DahuaDataUpdateCoordinator, config_entry):
        DahuaBaseEntity.__init__(self, coordinator, config_entry)
        SelectEntity.__init__(self)
        self._coordinator = coordinator
        self._attr_name = f"{coordinator.get_device_name()} Security Light"
        self._attr_unique_id = f"{coordinator.get_serial_number()}_security_light"
        self._attr_options = ["Off", "On", "Strobe"]

    @property
    def current_option(self) -> str:
        mode = self._coordinator.data.get("table.Lighting_V2[0][0][1].Mode", "")
        state = self._coordinator.data.get("table.Lighting_V2[0][0][1].State", "")
        if mode == "ForceOn" and state == "On":
            return "On"
        if mode == "ForceOn" and state == "Flicker":
            return "Strobe"
        return "Off"

    async def async_select_option(self, option: str) -> None:
        await self._coordinator.client.async_set_lighting_v2_for_amcrest_doorbells(option)
        await self._coordinator.async_refresh()

    @property
    def name(self):
        return self._attr_name

    @property
    def unique_id(self):
        return self._attr_unique_id


class DahuaCameraPresetPositionSelect(DahuaBaseEntity, SelectEntity):
    """Select a camera preset position."""

    def __init__(
        self, coordinator: DahuaDataUpdateCoordinator, config_entry,
        *, preset_ids: list[int] | None = None, rpc2_channel: int | None = None,
    ):
        DahuaBaseEntity.__init__(self, coordinator, config_entry)
        SelectEntity.__init__(self)
        self._coordinator = coordinator
        self._rpc2_channel = rpc2_channel
        self._attr_name = f"{coordinator.get_device_name()} Preset Position"
        suffix = "1_preset_position" if rpc2_channel == 1 else "preset_position"
        self._attr_unique_id = f"{coordinator.get_serial_number()}_{suffix}"
        if preset_ids is None:
            self._attr_options = ["Manual", "1", "2", "3", "4", "5", "6", "7", "8", "9", "10"]
        else:
            self._attr_options = ["Manual", *[str(value) for value in preset_ids]]

    @property
    def current_option(self) -> str:
        if self._rpc2_channel is not None:
            # This firmware has no supported CGI position readback. Do not claim
            # a position we cannot verify.
            return "Manual"
        preset_id = self._coordinator.data.get("status.PresetID", "0")
        if preset_id == "0":
            return "Manual"
        return preset_id

    async def async_select_option(self, option: str) -> None:
        if option == "Manual":
            return
        if self._rpc2_channel is not None:
            await self._coordinator.client.async_goto_preset_rpc2(
                self._rpc2_channel, int(option)
            )
        else:
            channel = self._coordinator.get_channel_number()
            await self._coordinator.client.async_goto_preset_position(channel, int(option))
        await self._coordinator.async_refresh()

    @property
    def name(self):
        return self._attr_name

    @property
    def unique_id(self):
        return self._attr_unique_id


class DahuaDayNightModeSelect(DahuaBaseEntity, SelectEntity):
    """The camera's Day/Night mode: colour, automatic, or black and white.

    #687 asked for this. The service to set it has existed for some time, but
    nothing read it back, so there was no way to notice a device that had
    changed mode by itself -- a VTO that reverts to Auto after a power cut, and
    then renders black and white at night, being the reported case.
    """

    _attr_options = ["Color", "Auto", "BlackWhite"]

    def __init__(self, coordinator: DahuaDataUpdateCoordinator, config_entry):
        super().__init__(coordinator, config_entry)
        self._coordinator = coordinator

    @property
    def name(self):
        return self._coordinator.get_device_name() + " Day/Night Mode"

    @property
    def unique_id(self):
        return self._coordinator.get_serial_number() + "_day_night_mode"

    @property
    def icon(self):
        return "mdi:theme-light-dark"

    @property
    def current_option(self):
        """The mode the device reports, or None when it has not reported one.

        None shows as unknown rather than as a mode the camera is not in, which
        is what a poll that has not landed yet, or a value outside the documented
        0/1/2, actually means.
        """
        return self._coordinator.get_day_night_color()

    async def async_select_option(self, option: str) -> None:
        if option not in self._attr_options:
            return
        await self._coordinator.client.async_set_video_in_day_night_mode(
            self._coordinator.get_channel(), "general", option)
        await self._coordinator.async_refresh()
