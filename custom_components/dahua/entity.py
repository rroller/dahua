"""DahuaBaseEntity class"""
from custom_components.dahua import DahuaDataUpdateCoordinator
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from .const import DOMAIN, ATTRIBUTION

"""
For a list of entity types, see https://developers.home-assistant.io/docs/core/entity/
"""
class DahuaBaseEntity(CoordinatorEntity):
    """
    DahuaBaseEntity is the base entity for all Dahua entities
    """

    def __init__(self, coordinator: DahuaDataUpdateCoordinator, config_entry):
        super().__init__(coordinator)
        self.config_entry = config_entry
        self._coordinator = coordinator

    # https://developers.home-assistant.io/docs/entity_registry_index
    @property
    def unique_id(self):
        """Return a unique ID to use for this entity."""
        return self._coordinator.get_serial_number()

    # https://developers.home-assistant.io/docs/device_registry_index
    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._coordinator.get_serial_number())},
            "name": self._coordinator.get_device_name(),
            "model": self._coordinator.get_model(),
            "manufacturer": "Dahua",
            "configuration_url": "http://" + self._coordinator.get_address(),
            "sw_version": self._coordinator.get_firmware_version(),
        }

    @property
    def extra_state_attributes(self):
        """Return the state attributes."""
        return {
            "attribution": ATTRIBUTION,
            "id": str(self.coordinator.data.get("id")),
            "integration": DOMAIN,
        }
