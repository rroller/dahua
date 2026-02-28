"""
Button entity platform for Dahua.
https://developers.home-assistant.io/docs/core/entity/button
Buttons require HomeAssistant 2021.12 or greater
"""

from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import DahuaConfigEntry


async def async_setup_entry(
    hass: HomeAssistant, entry: DahuaConfigEntry, async_add_devices: AddEntitiesCallback
) -> None:
    """Setup the button platform."""
    # TODO: Add some buttons. This requires a pretty recent version of HomeAssistant so I'm waiting a bit longer
    # before adding buttons
