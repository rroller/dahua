import logging
from typing import Any, Dict
from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .client import DahuaClient

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up Dahua duration sliders dynamically based on hardware dictionaries."""
    client: DahuaClient = hass.data[DOMAIN][entry.entry_id]["client"]
    
    # Retrieve raw capabilities cached during cold boot from the PR #811 implementation
    lighting_caps = client.get_product_definition("LightingControl")
    audio_caps = client.get_product_definition("AudioFileManager")
    
    # Extract channel from configuration entry context to prevent multi-channel collisions
    channel = entry.data.get("channel", 0)
    entities = []

    # 💡 LIGHT VALIDATION: Verify the parent key exists and is a valid dictionary
    if lighting_caps and isinstance(lighting_caps, dict):
        # If the camera lacks physical white-light hardware, this block is omitted or lacks ManualDetail
        if "ManualDetail" in lighting_caps or lighting_caps.get("result") is True:
            _LOGGER.info("Dahua lighting control dictionary detected for %s. Instantiating slider.", client.name)
            entities.append(DahuaDeterrenceLightDuration(client, channel))
        else:
            _LOGGER.debug("LightingControl key exists but physical deterrent white light is unsupported for %s.", client.name)

    # 💡 SIREN VALIDATION: Verify the core audio manager key is present in inherited records
    if audio_caps and isinstance(audio_caps, dict):
        # Legacy doorbells (like VTO models) won't have siren track wrappers or SirenFileManager properties
        if audio_caps.get("Support") is True or "SirenFileManager" in audio_caps:
            _LOGGER.info("Dahua audio manager dictionary detected for %s. Instantiating slider.", client.name)
            entities.append(DahuaDeterrenceSirenDuration(client, channel))
        else:
            _LOGGER.debug("AudioFileManager key present but core audio loop is omitted for %s.", client.name)

    # Register only defensive entities that passed our validation loops
    if entities:
        async_add_entities(entities, update_before_add=True)


class DahuaDeterrenceLightDuration(NumberEntity):
    """Exposes a visual slider to adjust the white light active window."""

    def __init__(self, client: DahuaClient, channel: int = 0) -> None:
        """Initialize the light duration entity."""
        self._client = client
        self._channel = channel
        self._attr_name = f"{client.name} Active Illumination Duration"
        self._attr_unique_id = f"dahua_{client.serial_number}_{channel}_light_manual_duration"
        self._value = 10  # Standard fallback state value initialization
        
        # Native Home Assistant UI Slider properties
        self._attr_mode = NumberMode.SLIDER
        self._attr_native_min_value = 10     # 10 seconds minimum fallback
        self._attr_native_max_value = 7200   # 2 hours maximum for user events (barbecues/landscaping)
        self._attr_native_step = 10          # Stepping resolution in seconds
        self._attr_icon = "mdi:lightbulb-timer"

    @property
    def native_value(self) -> float:
        """Return the current value of the entity."""
        return self._value

    async def async_set_native_value(self, value: float) -> None:
        """Update the physical camera configuration via client API write call."""
        target_seconds = int(value)
        _LOGGER.debug("Writing manual duration target to %s seconds", target_seconds)
        
        # Aligned to exact DahuaClient core writer definition
        success = await self._client.set_config(
            f"Lighting_V2[{self._channel}].ManualDetail.WhiteLight.ManualDuration", 
            target_seconds
        )
        if success:
            self._value = target_seconds
            self.async_write_ha_state()


class DahuaDeterrenceSirenDuration(NumberEntity):
    """Exposes a visual slider to adjust the siren loop repetitions."""

    def __init__(self, client: DahuaClient, channel: int = 0) -> None:
        """Initialize the siren repetitions entity."""
        self._client = client
        self._channel = channel
        self._attr_name = f"{client.name} Siren Playback Repetitions"
        self._attr_unique_id = f"dahua_{client.serial_number}_{channel}_siren_play_times"
        self._value = 2  # Standard repeat cycles value initialization
        
        # Configuration matches standard hardware synthesis iterations
        self._attr_mode = NumberMode.SLIDER
        self._attr_native_min_value = 1
        self._attr_native_max_value = 15      # Bounded by hardware audio synthesis limits
        self._attr_native_step = 1
        self._attr_icon = "mdi:volume-repeat"

    @property
    def native_value(self) -> float:
        """Return the current value of the entity."""
        return self._value

    async def async_set_native_value(self, value: float) -> None:
        """Update the physical camera configuration via client API write call."""
        target_repeats = int(value)
        _LOGGER.debug("Writing siren loop repetition target to %s iterations", target_repeats)
        
        # Aligned to exact DahuaClient core writer definition
        success = await self._client.set_config(
            f"AudioFileManager.VoicePlayTimesRange", 
            target_repeats
        )
        if success:
            self._value = target_repeats
            self.async_write_ha_state()
