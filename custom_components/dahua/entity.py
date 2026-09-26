"""DahuaBaseEntity class"""
from custom_components.dahua import DahuaDataUpdateCoordinator, async_host_is_unreachable
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
        info = {
            "identifiers": {(DOMAIN, self._coordinator.get_serial_number())},
            "name": self._coordinator.get_device_name(),
            "model": self._coordinator.get_model(),
            "manufacturer": "Dahua",
            "configuration_url": "http://" + self._coordinator.get_address(),
            "sw_version": self._coordinator.get_firmware_version(),
        }
        # Where the area chosen while adding this device is applied. Home
        # Assistant honours suggested_area only when it *creates* the device, so
        # this files a new one and never argues with a device the user has since
        # moved. Changing the area of an existing device is the options flow's
        # job, which moves it through the device registry instead.
        #
        # Omitted rather than passed as None: every key here is handed to
        # async_get_or_create as given.
        area = self._coordinator.configured_area_name()
        if area:
            info["suggested_area"] = area
        return info

    @property
    def extra_state_attributes(self):
        """Return the state attributes."""
        return {
            "id": str(self.coordinator.data.get("id")),
            "integration": DOMAIN,
        }


class DahuaEventDrivenEntity(DahuaBaseEntity):
    """An entity whose state is written when the device sends an event.

    These do not read the poll at all. They are pushed to, from the event
    stream, and they never subscribe to the coordinator -- their
    `async_added_to_hass` registers an event listener instead of calling
    `CoordinatorEntity`'s.

    They did still inherit `available` from it, which reports the result of the
    last poll. Home Assistant writes `unavailable` in place of whatever state an
    entity reports whenever that property is False, so a config read that timed
    out could turn the doorbell press that arrived a moment later into
    `unavailable` rather than `on`. The press was not missed -- it reached the
    bus and the listener ran -- but the state that automations trigger on never
    said so.

    The two transports are independent: the event stream can be connected and
    delivering while a `configManager.cgi` read is slow enough to time out. So
    judge these on whether the device has stopped answering altogether.
    """

    @property
    def available(self) -> bool:
        return not async_host_is_unreachable(self._coordinator.get_address())
