"""Tests for select platform."""

from unittest.mock import AsyncMock

import pytest

from custom_components.dahua.select import (
    DahuaCameraPresetPositionSelect,
    DahuaDoorbellLightSelect,
    async_setup_entry,
)


class TestAsyncSetupEntry:
    @pytest.mark.asyncio
    async def test_with_amcrest_doorbell(self, mock_coordinator, mock_config_entry):
        """Amcrest doorbell with security light gets doorbell light select."""
        mock_coordinator.model = "AD410"
        mock_config_entry.runtime_data = mock_coordinator
        added = []

        await async_setup_entry(None, mock_config_entry, added.append)

        types = [type(s).__name__ for sensors in added for s in sensors]
        assert "DahuaDoorbellLightSelect" in types
        assert "DahuaCameraPresetPositionSelect" in types

    @pytest.mark.asyncio
    async def test_without_doorbell(self, mock_coordinator, mock_config_entry):
        """Regular camera only gets preset position select."""
        mock_config_entry.runtime_data = mock_coordinator
        added = []

        await async_setup_entry(None, mock_config_entry, added.append)

        types = [type(s).__name__ for sensors in added for s in sensors]
        assert "DahuaDoorbellLightSelect" not in types
        assert "DahuaCameraPresetPositionSelect" in types


class TestDoorbellLightSelect:
    def test_unique_id(self, mock_coordinator, mock_config_entry):
        sel = DahuaDoorbellLightSelect(mock_coordinator, mock_config_entry)
        assert sel.unique_id == "SERIAL123_security_light"

    def test_name(self, mock_coordinator, mock_config_entry):
        sel = DahuaDoorbellLightSelect(mock_coordinator, mock_config_entry)
        assert sel.name == "Security Light"

    def test_current_option_on(self, mock_coordinator, mock_config_entry):
        mock_coordinator.data = {
            "table.Lighting_V2[0][0][1].Mode": "ForceOn",
            "table.Lighting_V2[0][0][1].State": "On",
        }
        sel = DahuaDoorbellLightSelect(mock_coordinator, mock_config_entry)
        assert sel.current_option == "On"

    def test_current_option_strobe(self, mock_coordinator, mock_config_entry):
        mock_coordinator.data = {
            "table.Lighting_V2[0][0][1].Mode": "ForceOn",
            "table.Lighting_V2[0][0][1].State": "Flicker",
        }
        sel = DahuaDoorbellLightSelect(mock_coordinator, mock_config_entry)
        assert sel.current_option == "Strobe"

    def test_current_option_off(self, mock_coordinator, mock_config_entry):
        mock_coordinator.data = {
            "table.Lighting_V2[0][0][1].Mode": "Off",
            "table.Lighting_V2[0][0][1].State": "Off",
        }
        sel = DahuaDoorbellLightSelect(mock_coordinator, mock_config_entry)
        assert sel.current_option == "Off"

    @pytest.mark.asyncio
    async def test_select_option(self, mock_coordinator, mock_config_entry):
        mock_coordinator.client.async_set_lighting_v2_for_amcrest_doorbells = (
            AsyncMock()
        )
        mock_coordinator.async_refresh = AsyncMock()
        sel = DahuaDoorbellLightSelect(mock_coordinator, mock_config_entry)

        await sel.async_select_option("On")
        mock_coordinator.client.async_set_lighting_v2_for_amcrest_doorbells.assert_called_once_with(
            "On"
        )


class TestPresetPositionSelect:
    def test_unique_id(self, mock_coordinator, mock_config_entry):
        sel = DahuaCameraPresetPositionSelect(mock_coordinator, mock_config_entry)
        assert sel.unique_id == "SERIAL123_preset_position"

    def test_current_option_manual(self, mock_coordinator, mock_config_entry):
        mock_coordinator.data = {"status.PresetID": "0"}
        sel = DahuaCameraPresetPositionSelect(mock_coordinator, mock_config_entry)
        assert sel.current_option == "Manual"

    def test_current_option_numbered(self, mock_coordinator, mock_config_entry):
        mock_coordinator.data = {"status.PresetID": "3"}
        sel = DahuaCameraPresetPositionSelect(mock_coordinator, mock_config_entry)
        assert sel.current_option == "3"

    @pytest.mark.asyncio
    async def test_select_option_manual_returns_early(
        self, mock_coordinator, mock_config_entry
    ):
        """Selecting Manual does nothing (early return)."""
        mock_coordinator.client.async_goto_preset_position = AsyncMock()
        sel = DahuaCameraPresetPositionSelect(mock_coordinator, mock_config_entry)

        await sel.async_select_option("Manual")
        mock_coordinator.client.async_goto_preset_position.assert_not_called()

    @pytest.mark.asyncio
    async def test_select_option_numbered(self, mock_coordinator, mock_config_entry):
        mock_coordinator.client.async_goto_preset_position = AsyncMock()
        mock_coordinator.async_refresh = AsyncMock()
        sel = DahuaCameraPresetPositionSelect(mock_coordinator, mock_config_entry)

        await sel.async_select_option("5")
        mock_coordinator.client.async_goto_preset_position.assert_called_once_with(0, 5)
