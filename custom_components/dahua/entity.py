"""DahuaBaseEntity class"""
from custom_components.dahua import DahuaDataUpdateCoordinator
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from .const import DOMAIN, ATTRIBUTION


class DahuaBaseEntity(CoordinatorEntity):
    """
    DahuaBaseEntity is the base entity for all Dahua entities
    """

    def __init__(self, coordinator: DahuaDataUpdateCoordinator, config_entry):
        super().__init__(coordinator)
        self.config_entry = config_entry

    # https://developers.home-assistant.io/docs/entity_registry_index
    @property
    def unique_id(self):
        """Return a unique ID to use for this entity."""
        return self.coordinator.get_serial_number()

    # https://developers.home-assistant.io/docs/device_registry_index
    @property
    def device_info(self):
        # self.coordinator.logger.error("%s", self.coordinator.data)
        return {
            "identifiers": {(DOMAIN, self.coordinator.get_serial_number())},
            "name": self.coordinator.get_device_name(),
            "model": self.coordinator.get_model(),
            "manufacturer": "Dahua",
            "sw_version": self.coordinator.get_firmware_version(),
        }

    @property
    def device_state_attributes(self):
        """Return the state attributes."""
        return {
            "attribution": ATTRIBUTION,
            "id": str(self.coordinator.data.get("id")),
            "integration": DOMAIN,
        }
