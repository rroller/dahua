"""Tests for __init__.py."""

import pytest

from custom_components.dahua import async_remove_config_entry_device


class TestRemoveConfigEntryDevice:
    @pytest.mark.asyncio
    async def test_allows_device_removal(self, hass, mock_config_entry):
        """async_remove_config_entry_device should return True to allow removal."""
        result = await async_remove_config_entry_device(hass, mock_config_entry, None)
        assert result is True
