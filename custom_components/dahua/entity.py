"""DahuaBaseEntity class"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
import functools
import socket
from typing import Any, TypeVar, cast

import aiohttp

from custom_components.dahua import DahuaDataUpdateCoordinator
from homeassistant.config_entries import ConfigEntry
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from .const import DOMAIN

_FuncT = TypeVar("_FuncT", bound=Callable[..., Coroutine[Any, Any, Any]])


def dahua_command(func: _FuncT) -> _FuncT:
    """Wrap async methods to raise HomeAssistantError on device communication failures."""

    @functools.wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return await func(*args, **kwargs)
        except (aiohttp.ClientError, socket.gaierror) as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="communication_error",
                translation_placeholders={"error": str(err)},
            ) from err
        except asyncio.TimeoutError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="timeout_error",
            ) from err
        except HomeAssistantError:
            raise
        except Exception as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="command_failed",
                translation_placeholders={"error": str(err)},
            ) from err

    return cast(_FuncT, wrapper)


"""
For a list of entity types, see https://developers.home-assistant.io/docs/core/entity/
"""


class DahuaBaseEntity(CoordinatorEntity[DahuaDataUpdateCoordinator]):
    """
    DahuaBaseEntity is the base entity for all Dahua entities
    """

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: DahuaDataUpdateCoordinator, config_entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator)
        self.config_entry = config_entry
        self._coordinator = coordinator

    # https://developers.home-assistant.io/docs/entity_registry_index
    @property
    def unique_id(self) -> str:
        """Return a unique ID to use for this entity."""
        return self._coordinator.get_serial_number()

    # https://developers.home-assistant.io/docs/device_registry_index
    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._coordinator.get_serial_number())},
            name=self._coordinator.get_device_name(),
            model=self._coordinator.get_model(),
            manufacturer="Dahua",
            configuration_url="http://" + self._coordinator.get_address(),
            sw_version=self._coordinator.get_firmware_version(),
            serial_number=self._coordinator.get_serial_number(),
        )

    @property
    def extra_state_attributes(self) -> dict[str, str]:
        """Return the state attributes."""
        return {
            "id": str(self.coordinator.data.get("id")),
            "integration": DOMAIN,
        }
