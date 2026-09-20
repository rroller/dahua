"""Regression tests for Dahua Smart Dual Light illuminator CGI handling."""

from unittest.mock import AsyncMock

import pytest

from custom_components.dahua.client import DahuaClient


def _client():
    """Build a client whose network access will be mocked per test."""
    return DahuaClient(
        "user",
        "password",
        "10.0.0.1",
        80,
        554,
        object(),
    )


# --- Lighting_V2 live-state detection ---------------------------------------


async def test_live_state_prefers_nearlight():
    c = _client()

    c.get = AsyncMock(
        return_value={
            "table.Lighting_V2[0][2][1].Mode": "Manual",
            "table.Lighting_V2[0][2][1].NearLight[0].Light": "88",
            "table.Lighting_V2[0][2][1].MiddleLight[0].Light": "44",
        }
    )

    result = await c.async_get_lighting_v2_live_state(0, "2", 1)

    assert result == ("Manual", "NearLight", 88)


async def test_live_state_uses_middlelight_when_nearlight_is_absent():
    c = _client()

    c.get = AsyncMock(
        return_value={
            "table.Lighting_V2[0][2][1].Mode": "Manual",
            "table.Lighting_V2[0][2][1].MiddleLight[0].Light": "73",
        }
    )

    result = await c.async_get_lighting_v2_live_state(0, "2", 1)

    assert result == ("Manual", "MiddleLight", 73)


async def test_live_state_falls_back_to_middlelight_with_no_brightness():
    c = _client()

    c.get = AsyncMock(
        return_value={
            "table.Lighting_V2[0][2][1].Mode": "Off",
        }
    )

    result = await c.async_get_lighting_v2_live_state(0, "2", 1)

    assert result == ("Off", "MiddleLight", None)


async def test_live_state_rejects_missing_mode():
    c = _client()

    c.get = AsyncMock(
        return_value={
            "table.Lighting_V2[0][2][1].NearLight[0].Light": "88",
        }
    )

    with pytest.raises(RuntimeError):
        await c.async_get_lighting_v2_live_state(0, "2", 1)


# --- LightingScheme CGI -----------------------------------------------------


async def test_get_lighting_scheme_reads_requested_channel_and_profile():
    c = _client()

    c.get = AsyncMock(
        return_value={
            "table.LightingScheme[3][2].LightingMode": "AIMode",
        }
    )

    mode = await c.async_get_lighting_scheme_mode(3, "2")

    assert mode == "AIMode"

    c.get.assert_awaited_once_with(
        "/cgi-bin/configManager.cgi?"
        "action=getConfig&name=LightingScheme"
    )


async def test_get_lighting_scheme_rejects_missing_mode():
    c = _client()
    c.get = AsyncMock(return_value={})

    with pytest.raises(ValueError):
        await c.async_get_lighting_scheme_mode(0, "2")


async def test_set_lighting_scheme_uses_persistent_cgi_and_returns_previous():
    c = _client()

    c.get = AsyncMock(
        side_effect=[
            {
                "table.LightingScheme[0][2].LightingMode": "AIMode",
            },
            {"OK": ""},
        ]
    )

    previous = await c.async_set_lighting_scheme(
        0,
        "2",
        "WhiteMode",
    )

    assert previous == "AIMode"

    assert c.get.await_count == 2

    assert c.get.await_args_list[1].args[0] == (
        "/cgi-bin/configManager.cgi?action=setConfig"
        "&LightingScheme[0][2].LightingMode=WhiteMode"
    )


async def test_set_lighting_scheme_skips_write_when_already_correct():
    c = _client()

    c.get = AsyncMock(
        return_value={
            "table.LightingScheme[0][2].LightingMode": "WhiteMode",
        }
    )

    previous = await c.async_set_lighting_scheme(
        0,
        "2",
        "WhiteMode",
    )

    assert previous == "WhiteMode"
    assert c.get.await_count == 1


# --- Lighting_V2 writes -----------------------------------------------------


async def test_set_lighting_v2_uses_nearlight_when_requested():
    c = _client()
    c.get = AsyncMock(return_value={"OK": ""})

    await c.async_set_lighting_v2(
        0,
        True,
        91,
        "2",
        light_index=1,
        bank="NearLight",
    )

    c.get.assert_awaited_once_with(
        "/cgi-bin/configManager.cgi?action=setConfig"
        "&Lighting_V2[0][2][1].Mode=Manual"
        "&Lighting_V2[0][2][1].NearLight[0].Light=91"
    )


async def test_set_lighting_v2_off_does_not_write_brightness():
    c = _client()
    c.get = AsyncMock(return_value={"OK": ""})

    await c.async_set_lighting_v2(
        0,
        False,
        91,
        "2",
        light_index=1,
        bank="NearLight",
    )

    c.get.assert_awaited_once_with(
        "/cgi-bin/configManager.cgi?action=setConfig"
        "&Lighting_V2[0][2][1].Mode=Off"
    )


async def test_raw_restore_preserves_nearlight_and_exact_mode():
    c = _client()
    c.get = AsyncMock(return_value={"OK": ""})

    await c.async_set_lighting_v2_raw(
        0,
        "2",
        1,
        "Manual",
        "NearLight",
        88,
    )

    c.get.assert_awaited_once_with(
        "/cgi-bin/configManager.cgi?action=setConfig"
        "&Lighting_V2[0][2][1].Mode=Manual"
        "&Lighting_V2[0][2][1].NearLight[0].Light=88"
    )


async def test_raw_off_can_be_written_without_brightness():
    c = _client()
    c.get = AsyncMock(return_value={"OK": ""})

    await c.async_set_lighting_v2_raw(
        0,
        "2",
        1,
        "Off",
        "NearLight",
    )

    c.get.assert_awaited_once_with(
        "/cgi-bin/configManager.cgi?action=setConfig"
        "&Lighting_V2[0][2][1].Mode=Off"
    )


async def test_uptime_rejects_when_rpc2_disabled():
    """Uptime probing must not create RPC2 when the option is disabled."""
    c = _client()
    c._shared_rpc2 = AsyncMock()

    with pytest.raises(RuntimeError, match="RPC2 is disabled"):
        await c.async_get_uptime_last()

    c._shared_rpc2.assert_not_awaited()


async def test_raw_off_can_restore_original_brightness():
    """Exact restore may need Mode=Off together with saved brightness."""
    c = _client()
    c.get = AsyncMock(return_value={"OK": ""})

    await c.async_set_lighting_v2_raw(
        0,
        "2",
        1,
        "Off",
        "NearLight",
        30,
    )

    c.get.assert_awaited_once_with(
        "/cgi-bin/configManager.cgi?action=setConfig"
        "&Lighting_V2[0][2][1].Mode=Off"
        "&Lighting_V2[0][2][1].NearLight[0].Light=30"
    )

