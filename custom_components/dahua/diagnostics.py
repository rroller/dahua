"""Diagnostics support for Dahua."""

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import DahuaConfigEntry
from .const import CONF_PASSWORD, CONF_USERNAME

TO_REDACT = {CONF_PASSWORD, CONF_USERNAME}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: DahuaConfigEntry
) -> dict:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data

    return {
        "config_entry": async_redact_data(dict(entry.data), TO_REDACT),
        "model": coordinator.get_model(),
        "serial_number": coordinator.get_serial_number(),
        "firmware": coordinator.get_firmware_version(),
        "device_name": coordinator.get_device_name(),
        "supports": {
            "infrared_light": coordinator.supports_infrared_light(),
            "illuminator": coordinator.supports_illuminator(),
            "security_light": coordinator.supports_security_light(),
            "siren": coordinator.supports_siren(),
            "smart_motion_detection": coordinator.supports_smart_motion_detection(),
            "flood_light": coordinator.is_flood_light(),
            "doorbell": coordinator.is_doorbell(),
        },
    }
