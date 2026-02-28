"""Tests for light platform."""

from unittest.mock import AsyncMock

import pytest
from homeassistant.components.light import ATTR_BRIGHTNESS, ColorMode

from custom_components.dahua.light import (
    AmcrestRingLight,
    DahuaIlluminator,
    DahuaInfraredLight,
    DahuaSecurityLight,
    FloodLight,
    async_setup_entry,
)


class TestAsyncSetupEntry:
    @pytest.mark.asyncio
    async def test_infrared_light(self, mock_coordinator, mock_config_entry):
        """Infrared light is added when supported."""
        mock_coordinator._supports_lighting = True
        mock_coordinator.model = "IPC-HDW5831R-ZE"
        mock_config_entry.runtime_data = mock_coordinator
        added = []

        await async_setup_entry(None, mock_config_entry, added.extend)

        types = [type(e).__name__ for e in added]
        assert "DahuaInfraredLight" in types

    @pytest.mark.asyncio
    async def test_illuminator(self, mock_coordinator, mock_config_entry):
        """Illuminator added when Lighting_V2 data present."""
        mock_coordinator._supports_lighting_v2 = True
        mock_coordinator.data = {"table.Lighting_V2[0][0][0].Mode": "Off"}
        mock_config_entry.runtime_data = mock_coordinator
        added = []

        await async_setup_entry(None, mock_config_entry, added.extend)

        types = [type(e).__name__ for e in added]
        assert "DahuaIlluminator" in types

    @pytest.mark.asyncio
    async def test_flood_light(self, mock_coordinator, mock_config_entry):
        """Flood light added for flood light models."""
        mock_coordinator.model = "ASH26-W"
        mock_config_entry.runtime_data = mock_coordinator
        added = []

        await async_setup_entry(None, mock_config_entry, added.extend)

        types = [type(e).__name__ for e in added]
        assert "FloodLight" in types

    @pytest.mark.asyncio
    async def test_security_light(self, mock_coordinator, mock_config_entry):
        """Security light added for AS-PV models (non-Amcrest doorbell)."""
        mock_coordinator.model = "IPC-HDW3849HP-AS-PV"
        mock_config_entry.runtime_data = mock_coordinator
        added = []

        await async_setup_entry(None, mock_config_entry, added.extend)

        types = [type(e).__name__ for e in added]
        assert "DahuaSecurityLight" in types

    @pytest.mark.asyncio
    async def test_amcrest_ring_light(self, mock_coordinator, mock_config_entry):
        """Amcrest doorbell gets ring light."""
        mock_coordinator.model = "AD410"
        mock_config_entry.runtime_data = mock_coordinator
        added = []

        await async_setup_entry(None, mock_config_entry, added.extend)

        types = [type(e).__name__ for e in added]
        assert "AmcrestRingLight" in types

    @pytest.mark.asyncio
    async def test_no_lights(self, mock_coordinator, mock_config_entry):
        """No lights if no features supported."""
        mock_coordinator._supports_lighting = False
        mock_coordinator.model = "IPC-HDW5831R-ZE"
        mock_coordinator.data = {}
        mock_config_entry.runtime_data = mock_coordinator
        added = []

        await async_setup_entry(None, mock_config_entry, added.extend)

        assert len(added) == 0


class TestDahuaInfraredLight:
    def test_unique_id(self, mock_coordinator, mock_config_entry):
        light = DahuaInfraredLight(mock_coordinator, mock_config_entry)
        assert light.unique_id == "SERIAL123_infrared"

    def test_translation_key(self, mock_coordinator, mock_config_entry):
        light = DahuaInfraredLight(mock_coordinator, mock_config_entry)
        assert light._attr_translation_key == "infrared"

    def test_is_on(self, mock_coordinator, mock_config_entry):
        mock_coordinator.data = {"table.Lighting[0][0].Mode": "Manual"}
        light = DahuaInfraredLight(mock_coordinator, mock_config_entry)
        assert light.is_on is True

    def test_brightness(self, mock_coordinator, mock_config_entry):
        mock_coordinator.data = {"table.Lighting[0][0].MiddleLight[0].Light": "100"}
        light = DahuaInfraredLight(mock_coordinator, mock_config_entry)
        assert light.brightness == 255

    def test_color_mode(self, mock_coordinator, mock_config_entry):
        light = DahuaInfraredLight(mock_coordinator, mock_config_entry)
        assert light.color_mode == ColorMode.BRIGHTNESS

    def test_supported_color_modes(self, mock_coordinator, mock_config_entry):
        light = DahuaInfraredLight(mock_coordinator, mock_config_entry)
        assert light.supported_color_modes == {ColorMode.BRIGHTNESS}

    def test_supported_features(self, mock_coordinator, mock_config_entry):
        from homeassistant.components.light import LightEntityFeature

        light = DahuaInfraredLight(mock_coordinator, mock_config_entry)
        assert light.supported_features == LightEntityFeature.EFFECT

    def test_should_poll(self, mock_coordinator, mock_config_entry):
        light = DahuaInfraredLight(mock_coordinator, mock_config_entry)
        assert light.should_poll is False

    @pytest.mark.asyncio
    async def test_turn_on(self, mock_coordinator, mock_config_entry):
        mock_coordinator.client.async_set_lighting_v1 = AsyncMock()
        mock_coordinator.async_refresh = AsyncMock()
        light = DahuaInfraredLight(mock_coordinator, mock_config_entry)

        await light.async_turn_on(**{ATTR_BRIGHTNESS: 255})
        mock_coordinator.client.async_set_lighting_v1.assert_called_once_with(
            0, True, 100
        )

    @pytest.mark.asyncio
    async def test_turn_off(self, mock_coordinator, mock_config_entry):
        mock_coordinator.client.async_set_lighting_v1 = AsyncMock()
        mock_coordinator.async_refresh = AsyncMock()
        light = DahuaInfraredLight(mock_coordinator, mock_config_entry)

        await light.async_turn_off()
        mock_coordinator.client.async_set_lighting_v1.assert_called_once()
        call_args = mock_coordinator.client.async_set_lighting_v1.call_args
        assert call_args[0][1] is False


class TestDahuaIlluminator:
    def test_unique_id(self, mock_coordinator, mock_config_entry):
        light = DahuaIlluminator(mock_coordinator, mock_config_entry)
        assert light.unique_id == "SERIAL123_illuminator"

    def test_translation_key(self, mock_coordinator, mock_config_entry):
        light = DahuaIlluminator(mock_coordinator, mock_config_entry)
        assert light._attr_translation_key == "illuminator"

    def test_brightness(self, mock_coordinator, mock_config_entry):
        mock_coordinator._profile_mode = "0"
        mock_coordinator.data = {
            "table.Lighting_V2[0][0][0].MiddleLight[0].Light": "50"
        }
        light = DahuaIlluminator(mock_coordinator, mock_config_entry)
        assert light.brightness == 127

    def test_color_mode(self, mock_coordinator, mock_config_entry):
        light = DahuaIlluminator(mock_coordinator, mock_config_entry)
        assert light.color_mode == ColorMode.BRIGHTNESS

    def test_supported_color_modes(self, mock_coordinator, mock_config_entry):
        light = DahuaIlluminator(mock_coordinator, mock_config_entry)
        assert light.supported_color_modes == {ColorMode.BRIGHTNESS}

    def test_should_poll(self, mock_coordinator, mock_config_entry):
        light = DahuaIlluminator(mock_coordinator, mock_config_entry)
        assert light.should_poll is False

    def test_is_on(self, mock_coordinator, mock_config_entry):
        mock_coordinator._profile_mode = "0"
        mock_coordinator.data = {"table.Lighting_V2[0][0][0].Mode": "Manual"}
        light = DahuaIlluminator(mock_coordinator, mock_config_entry)
        assert light.is_on is True

    @pytest.mark.asyncio
    async def test_turn_on(self, mock_coordinator, mock_config_entry):
        mock_coordinator.client.async_set_lighting_v2 = AsyncMock()
        mock_coordinator.async_refresh = AsyncMock()
        light = DahuaIlluminator(mock_coordinator, mock_config_entry)

        await light.async_turn_on(**{ATTR_BRIGHTNESS: 128})
        mock_coordinator.client.async_set_lighting_v2.assert_called_once()
        call_args = mock_coordinator.client.async_set_lighting_v2.call_args
        assert call_args[0][1] is True  # enabled

    @pytest.mark.asyncio
    async def test_turn_off(self, mock_coordinator, mock_config_entry):
        mock_coordinator.client.async_set_lighting_v2 = AsyncMock()
        mock_coordinator.async_refresh = AsyncMock()
        light = DahuaIlluminator(mock_coordinator, mock_config_entry)

        await light.async_turn_off()
        call_args = mock_coordinator.client.async_set_lighting_v2.call_args
        assert call_args[0][1] is False  # enabled


class TestAmcrestRingLight:
    def test_unique_id(self, mock_coordinator, mock_config_entry):
        light = AmcrestRingLight(mock_coordinator, mock_config_entry)
        assert light.unique_id == "SERIAL123_ring_light"

    def test_translation_key(self, mock_coordinator, mock_config_entry):
        light = AmcrestRingLight(mock_coordinator, mock_config_entry)
        assert light._attr_translation_key == "ring_light"

    def test_is_on(self, mock_coordinator, mock_config_entry):
        mock_coordinator.data = {"table.LightGlobal[0].Enable": "true"}
        light = AmcrestRingLight(mock_coordinator, mock_config_entry)
        assert light.is_on is True

    def test_color_mode(self, mock_coordinator, mock_config_entry):
        light = AmcrestRingLight(mock_coordinator, mock_config_entry)
        assert light.color_mode == ColorMode.ONOFF

    @pytest.mark.asyncio
    async def test_turn_on(self, mock_coordinator, mock_config_entry):
        mock_coordinator.client.async_set_light_global_enabled = AsyncMock()
        mock_coordinator.async_refresh = AsyncMock()
        light = AmcrestRingLight(mock_coordinator, mock_config_entry)

        await light.async_turn_on()
        mock_coordinator.client.async_set_light_global_enabled.assert_called_once_with(
            True
        )

    @pytest.mark.asyncio
    async def test_turn_off(self, mock_coordinator, mock_config_entry):
        mock_coordinator.client.async_set_light_global_enabled = AsyncMock()
        mock_coordinator.async_refresh = AsyncMock()
        light = AmcrestRingLight(mock_coordinator, mock_config_entry)

        await light.async_turn_off()
        mock_coordinator.client.async_set_light_global_enabled.assert_called_once_with(
            False
        )


class TestFloodLight:
    def test_unique_id(self, mock_coordinator, mock_config_entry):
        light = FloodLight(mock_coordinator, mock_config_entry)
        assert light.unique_id == "SERIAL123_flood_light"

    def test_translation_key(self, mock_coordinator, mock_config_entry):
        light = FloodLight(mock_coordinator, mock_config_entry)
        assert light._attr_translation_key == "flood_light"

    def test_color_mode(self, mock_coordinator, mock_config_entry):
        light = FloodLight(mock_coordinator, mock_config_entry)
        assert light.color_mode == ColorMode.ONOFF

    def test_supported_color_modes(self, mock_coordinator, mock_config_entry):
        light = FloodLight(mock_coordinator, mock_config_entry)
        assert light.supported_color_modes == {ColorMode.ONOFF}

    def test_supported_features(self, mock_coordinator, mock_config_entry):
        from homeassistant.components.light import LightEntityFeature

        light = FloodLight(mock_coordinator, mock_config_entry)
        assert light.supported_features == LightEntityFeature.EFFECT

    def test_should_poll(self, mock_coordinator, mock_config_entry):
        light = FloodLight(mock_coordinator, mock_config_entry)
        assert light.should_poll is False

    def test_is_on_with_floodlightmode(self, mock_coordinator, mock_config_entry):
        mock_coordinator._supports_floodlightmode = True
        mock_coordinator.data = {"status.status.WhiteLight": "On"}
        light = FloodLight(mock_coordinator, mock_config_entry)
        assert light.is_on is True

    @pytest.mark.asyncio
    async def test_turn_on_with_floodlightmode(
        self, mock_coordinator, mock_config_entry
    ):
        mock_coordinator._supports_floodlightmode = True
        mock_coordinator.client.async_get_floodlightmode = AsyncMock(return_value=2)
        mock_coordinator.client.async_set_floodlightmode = AsyncMock()
        mock_coordinator.client.async_set_coaxial_control_state = AsyncMock()
        mock_coordinator.async_refresh = AsyncMock()
        light = FloodLight(mock_coordinator, mock_config_entry)

        await light.async_turn_on()
        mock_coordinator.client.async_set_floodlightmode.assert_called_once_with(2)
        mock_coordinator.client.async_set_coaxial_control_state.assert_called_once()

    @pytest.mark.asyncio
    async def test_turn_off_with_floodlightmode(
        self, mock_coordinator, mock_config_entry
    ):
        mock_coordinator._supports_floodlightmode = True
        mock_coordinator._floodlight_mode = 2
        mock_coordinator.client.async_set_floodlightmode = AsyncMock()
        mock_coordinator.client.async_set_coaxial_control_state = AsyncMock()
        mock_coordinator.async_refresh = AsyncMock()
        light = FloodLight(mock_coordinator, mock_config_entry)

        await light.async_turn_off()
        mock_coordinator.client.async_set_coaxial_control_state.assert_called_once()
        mock_coordinator.client.async_set_floodlightmode.assert_called_once_with(2)

    @pytest.mark.asyncio
    async def test_turn_on_without_floodlightmode(
        self, mock_coordinator, mock_config_entry
    ):
        mock_coordinator._supports_floodlightmode = False
        mock_coordinator.client.async_set_lighting_v2_for_flood_lights = AsyncMock()
        mock_coordinator.async_refresh = AsyncMock()
        light = FloodLight(mock_coordinator, mock_config_entry)

        await light.async_turn_on()
        mock_coordinator.client.async_set_lighting_v2_for_flood_lights.assert_called_once()
        call_args = (
            mock_coordinator.client.async_set_lighting_v2_for_flood_lights.call_args
        )
        assert call_args[0][1] is True  # enabled

    @pytest.mark.asyncio
    async def test_turn_off_without_floodlightmode(
        self, mock_coordinator, mock_config_entry
    ):
        mock_coordinator._supports_floodlightmode = False
        mock_coordinator.client.async_set_lighting_v2_for_flood_lights = AsyncMock()
        mock_coordinator.async_refresh = AsyncMock()
        light = FloodLight(mock_coordinator, mock_config_entry)

        await light.async_turn_off()
        call_args = (
            mock_coordinator.client.async_set_lighting_v2_for_flood_lights.call_args
        )
        assert call_args[0][1] is False  # enabled


class TestAmcrestRingLightExtended:
    def test_supported_color_modes(self, mock_coordinator, mock_config_entry):
        light = AmcrestRingLight(mock_coordinator, mock_config_entry)
        assert light.supported_color_modes == {ColorMode.ONOFF}


class TestDahuaSecurityLight:
    def test_unique_id(self, mock_coordinator, mock_config_entry):
        light = DahuaSecurityLight(mock_coordinator, mock_config_entry)
        assert light.unique_id == "SERIAL123_security"

    def test_translation_key(self, mock_coordinator, mock_config_entry):
        light = DahuaSecurityLight(mock_coordinator, mock_config_entry)
        assert light._attr_translation_key == "security_light"

    def test_should_poll(self, mock_coordinator, mock_config_entry):
        light = DahuaSecurityLight(mock_coordinator, mock_config_entry)
        assert light.should_poll is False

    def test_supported_color_modes(self, mock_coordinator, mock_config_entry):
        light = DahuaSecurityLight(mock_coordinator, mock_config_entry)
        assert light.supported_color_modes == {ColorMode.ONOFF}

    def test_is_on(self, mock_coordinator, mock_config_entry):
        mock_coordinator.data = {"status.status.WhiteLight": "On"}
        light = DahuaSecurityLight(mock_coordinator, mock_config_entry)
        assert light.is_on is True

    def test_color_mode(self, mock_coordinator, mock_config_entry):
        light = DahuaSecurityLight(mock_coordinator, mock_config_entry)
        assert light.color_mode == ColorMode.ONOFF

    @pytest.mark.asyncio
    async def test_turn_on(self, mock_coordinator, mock_config_entry):
        mock_coordinator.client.async_set_coaxial_control_state = AsyncMock()
        mock_coordinator.async_refresh = AsyncMock()
        light = DahuaSecurityLight(mock_coordinator, mock_config_entry)

        await light.async_turn_on()
        mock_coordinator.client.async_set_coaxial_control_state.assert_called_once_with(
            0, 1, True
        )

    @pytest.mark.asyncio
    async def test_turn_off(self, mock_coordinator, mock_config_entry):
        mock_coordinator.client.async_set_coaxial_control_state = AsyncMock()
        mock_coordinator.async_refresh = AsyncMock()
        light = DahuaSecurityLight(mock_coordinator, mock_config_entry)

        await light.async_turn_off()
        mock_coordinator.client.async_set_coaxial_control_state.assert_called_once_with(
            0, 1, False
        )
