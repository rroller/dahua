"""
Select entity platform for dahua.
https://developers.home-assistant.io/docs/core/entity/select
Requires HomeAssistant 2021.7.0 or greater
"""

from homeassistant.core import HomeAssistant
from homeassistant.components.select import SelectEntity
from custom_components.dahua import DahuaConfigEntry, DahuaDataUpdateCoordinator
from .entity import DahuaBaseEntity, dahua_command

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant, entry: DahuaConfigEntry, async_add_devices
):
    """Setup select platform."""
    coordinator: DahuaDataUpdateCoordinator = entry.runtime_data

    devices = []

    if coordinator.is_amcrest_doorbell() and coordinator.supports_security_light():
        devices.append(DahuaDoorbellLightSelect(coordinator, entry))

    # if coordinator._supports_ptz_position:
    devices.append(DahuaCameraPresetPositionSelect(coordinator, entry))

    async_add_devices(devices)


class DahuaDoorbellLightSelect(DahuaBaseEntity, SelectEntity):
    """allows one to turn the doorbell light on/off/strobe"""

    def __init__(self, coordinator: DahuaDataUpdateCoordinator, config_entry):
        DahuaBaseEntity.__init__(self, coordinator, config_entry)
        SelectEntity.__init__(self)
        self._coordinator = coordinator
        self._attr_name = "Security Light"
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

    @dahua_command
    async def async_select_option(self, option: str) -> None:
        await self._coordinator.client.async_set_lighting_v2_for_amcrest_doorbells(
            option
        )
        await self._coordinator.async_refresh()

    @property
    def name(self):
        return self._attr_name

    @property
    def unique_id(self):
        """https://developers.home-assistant.io/docs/entity_registry_index/#unique-id-requirements"""
        return self._attr_unique_id


class DahuaCameraPresetPositionSelect(DahuaBaseEntity, SelectEntity):
    """allows"""

    def __init__(self, coordinator: DahuaDataUpdateCoordinator, config_entry):
        DahuaBaseEntity.__init__(self, coordinator, config_entry)
        SelectEntity.__init__(self)
        self._coordinator = coordinator
        self._attr_name = "Preset Position"
        self._attr_unique_id = f"{coordinator.get_serial_number()}_preset_position"
        self._attr_options = [
            "Manual",
            "1",
            "2",
            "3",
            "4",
            "5",
            "6",
            "7",
            "8",
            "9",
            "10",
        ]

    @property
    def current_option(self) -> str:
        presetID = self._coordinator.data.get("status.PresetID", "0")
        if presetID == "0":
            return "Manual"
        return presetID

    @dahua_command
    async def async_select_option(self, option: str) -> None:
        if option == "Manual":
            return
        channel = self._coordinator.get_channel()
        await self._coordinator.client.async_goto_preset_position(channel, int(option))
        await self._coordinator.async_refresh()

    @property
    def name(self):
        return self._attr_name

    @property
    def unique_id(self):
        """https://developers.home-assistant.io/docs/entity_registry_index/#unique-id-requirements"""
        return self._attr_unique_id
