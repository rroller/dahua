"""Tests for button platform."""

import pytest

from custom_components.dahua.button import async_setup_entry


class TestAsyncSetupEntry:
    @pytest.mark.asyncio
    async def test_setup_adds_nothing(self, hass, mock_config_entry):
        """Button platform currently adds no entities."""
        added = []
        await async_setup_entry(hass, mock_config_entry, added.append)
        assert len(added) == 0
