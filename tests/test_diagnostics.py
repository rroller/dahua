"""Tests for diagnostics."""

import pytest

from custom_components.dahua.diagnostics import async_get_config_entry_diagnostics


class TestDiagnostics:
    @pytest.mark.asyncio
    async def test_diagnostics_output(self, hass, mock_coordinator, mock_config_entry):
        """Test diagnostics returns expected data with redacted credentials."""
        mock_config_entry.runtime_data = mock_coordinator
        mock_coordinator.data = {"version": "2.800.0000016.0.R,build:2020-06-05"}

        result = await async_get_config_entry_diagnostics(hass, mock_config_entry)

        # Config entry data should be present but credentials redacted
        assert result["config_entry"]["address"] == "192.168.1.108"
        assert result["config_entry"]["username"] == "**REDACTED**"
        assert result["config_entry"]["password"] == "**REDACTED**"

        # Device info should be present
        assert result["model"] == "IPC-HDW5831R-ZE"
        assert result["serial_number"] == "SERIAL123"
        assert result["device_name"] == "TestCam"

        # Supports dict should be present
        assert "infrared_light" in result["supports"]
        assert "illuminator" in result["supports"]
        assert "security_light" in result["supports"]
        assert "siren" in result["supports"]
        assert "smart_motion_detection" in result["supports"]
        assert "flood_light" in result["supports"]
        assert "doorbell" in result["supports"]
