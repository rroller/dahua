"""NUmber platform for dahua."""
from homeassistant.core import HomeAssistant
from homeassistant.components.Number import NumberEntity
from homeassistant.helpers import entity_platform
from custom_components.dahua import DahuaDataUpdateCoordinator

from .const import DOMAIN, ZOOM_ICON, FOCUS_ICON
from .entity import DahuaBaseEntity

SERVICE_AUTOFOCUS = "autofocus"

async def async_setup_entry(hass: HomeAssistant, entry, async_add_devices):
    """Setup sensor platform."""
    coordinator: DahuaDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]

    # But only some cams have a siren, very few do actually
    if coordinator.supports_focus_zoom():
        async_add_devices([DahuaCameraZoomNumber(coordinator, entry), DahuaCameraFocusNumber(coordinator, entry)])
        platform = entity_platform.async_get_current_platform()
        platform.async_register_entity_service(
            SERVICE_AUTOFOCUS,
            {},
            "async_auto_focus"
        )

class DahuaCameraZoomNumber(DahuaBaseEntity, NumberEntity):
    """dahua Camera Zoom"""

    async def async_set_native_value(self, value: float) -> None: 
        """Turn off/disable motion detection."""
        await self._coordinator.client.async_set_zoom_v1(value)
        await self._coordinator.async_refresh()

    @property
    def name(self):
        return self._coordinator.get_device_name() + " Zoom"

    @property
    def unique_id(self):
        """
        A unique identifier for this entity. Needs to be unique within a platform (ie light.hue). Should not be configurable by the user or be changeable
        see https://developers.home-assistant.io/docs/entity_registry_index/#unique-id-requirements
        """
        return self._coordinator.get_serial_number() + "_zoom"

    @property
    def icon(self):
        """Return the icon of this switch."""
        return ZOOM_ICON

    @property
    def native_value(self):
        """
        Value is fetched from api.getFocusStatus
        """
        return self._coordinator.get_zoom()
    
    @property
    def native_max_value(self):
        return 1
    
    @property
    def native_step(self):
        return 0.000001
    
    @property
    def native_unit_of_measurement(self):
        return "%"
    
    
class DahuaCameraFocusNumber(DahuaBaseEntity, NumberEntity):
    """dahua Camera Focus"""

    async def async_set_native_value(self, value: float) -> None: 
        """Turn off/disable motion detection."""
        await self._coordinator.client.async_set_focus_v1(value)
        await self._coordinator.async_refresh()

    @property
    def name(self):
        return self._coordinator.get_device_name() + " Focus"

    @property
    def unique_id(self):
        """
        A unique identifier for this entity. Needs to be unique within a platform (ie light.hue). Should not be configurable by the user or be changeable
        see https://developers.home-assistant.io/docs/entity_registry_index/#unique-id-requirements
        """
        return self._coordinator.get_serial_number() + "_focus"

    @property
    def icon(self):
        """Return the icon of this Number."""
        return FOCUS_ICON

    @property
    def native_value(self):
        """
        Value is fetched from api.getFocusStatus
        """
        return self._coordinator.get_focus()
    
    @property
    def native_max_value(self):
        return 1
    
    @property
    def native_step(self):
        return 0.000001
    
    @property
    def native_unit_of_measurement(self):
        return "%"
    
    async def async_auto_focus(self):
        """ Handles the service call from SERVICE_SET_INFRARED_MODE to set zoom and focus """
        await self._coordinator.client.async_auto_focus_v1()
        await self._coordinator.async_refresh()
