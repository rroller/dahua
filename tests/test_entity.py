"""Tests for entity module (dahua_command decorator and DahuaBaseEntity)."""

import asyncio
import socket

import aiohttp
import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.dahua.const import DOMAIN
from custom_components.dahua.entity import DahuaBaseEntity, dahua_command

# --- dahua_command decorator tests ---


@dahua_command
async def _dummy_success(**kwargs):
    return "ok"


@dahua_command
async def _dummy_client_error(**kwargs):
    raise aiohttp.ClientError("connection refused")


@dahua_command
async def _dummy_gaierror(**kwargs):
    raise socket.gaierror("name resolution failed")


@dahua_command
async def _dummy_timeout(**kwargs):
    raise asyncio.TimeoutError()


@dahua_command
async def _dummy_ha_error(**kwargs):
    raise HomeAssistantError("existing error")


@dahua_command
async def _dummy_generic_error(**kwargs):
    raise ValueError("something broke")


@pytest.mark.asyncio
async def test_dahua_command_success():
    result = await _dummy_success()
    assert result == "ok"


@pytest.mark.asyncio
async def test_dahua_command_client_error():
    with pytest.raises(HomeAssistantError) as exc_info:
        await _dummy_client_error()
    assert exc_info.value.translation_domain == DOMAIN
    assert exc_info.value.translation_key == "communication_error"
    assert exc_info.value.translation_placeholders["error"] == "connection refused"


@pytest.mark.asyncio
async def test_dahua_command_gaierror():
    with pytest.raises(HomeAssistantError) as exc_info:
        await _dummy_gaierror()
    assert exc_info.value.translation_domain == DOMAIN
    assert exc_info.value.translation_key == "communication_error"


@pytest.mark.asyncio
async def test_dahua_command_timeout():
    with pytest.raises(HomeAssistantError) as exc_info:
        await _dummy_timeout()
    assert exc_info.value.translation_domain == DOMAIN
    assert exc_info.value.translation_key == "timeout_error"


@pytest.mark.asyncio
async def test_dahua_command_ha_error_passthrough():
    with pytest.raises(HomeAssistantError, match="existing error"):
        await _dummy_ha_error()


@pytest.mark.asyncio
async def test_dahua_command_generic_error():
    with pytest.raises(HomeAssistantError) as exc_info:
        await _dummy_generic_error()
    assert exc_info.value.translation_domain == DOMAIN
    assert exc_info.value.translation_key == "command_failed"
    assert exc_info.value.translation_placeholders["error"] == "something broke"


# --- DahuaBaseEntity tests ---


def test_base_entity_unique_id(mock_coordinator, mock_config_entry):
    entity = DahuaBaseEntity(mock_coordinator, mock_config_entry)
    assert entity.unique_id == "SERIAL123"


def test_base_entity_device_info(mock_coordinator, mock_config_entry):
    entity = DahuaBaseEntity(mock_coordinator, mock_config_entry)
    info = entity.device_info
    assert info["identifiers"] == {(DOMAIN, "SERIAL123")}
    assert info["name"] == "TestCam"
    assert info["model"] == "IPC-HDW5831R-ZE"
    assert info["manufacturer"] == "Dahua"
    assert "192.168.1.108" in info["configuration_url"]
    assert info["serial_number"] == "SERIAL123"


def test_base_entity_extra_state_attributes(mock_coordinator, mock_config_entry):
    entity = DahuaBaseEntity(mock_coordinator, mock_config_entry)
    attrs = entity.extra_state_attributes
    assert attrs["integration"] == DOMAIN


def test_base_entity_has_entity_name(mock_coordinator, mock_config_entry):
    entity = DahuaBaseEntity(mock_coordinator, mock_config_entry)
    assert entity._attr_has_entity_name is True
