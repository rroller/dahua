"""Tests for active deterrence control on NVR channels."""

from unittest.mock import AsyncMock

from custom_components.dahua.client import DahuaClient


async def test_nvr_coaxial_control_state_uses_required_parameters():
    client = DahuaClient("u", "p", "nvr", 80, 554, None)
    client.get = AsyncMock(return_value={})

    await client.async_set_nvr_coaxial_control_state(3, 1, True)
    assert client.get.await_args.args == (
        "/cgi-bin/coaxialControlIO.cgi?action=control&channel=3"
        "&info[0].Type=1&info[0].IO=1&info[0].TriggerMode=2",
    )

    await client.async_set_nvr_coaxial_control_state(3, 1, False)
    assert client.get.await_args.args == (
        "/cgi-bin/coaxialControlIO.cgi?action=control&channel=3"
        "&info[0].Type=1&info[0].IO=2&info[0].TriggerMode=2",
    )

    await client.async_set_nvr_coaxial_control_state(3, 2, True)
    assert client.get.await_args.args == (
        "/cgi-bin/coaxialControlIO.cgi?action=control&channel=3"
        "&info[0].Type=2&info[0].IO=1&info[0].TriggerMode=2",
    )
