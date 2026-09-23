"""Security-light support for the dual-sensor PTZ3E10X-T180."""

from unittest.mock import AsyncMock

from custom_components.dahua import DahuaDataUpdateCoordinator
from custom_components.dahua.client import SECURITY_LIGHT_TYPE, DahuaClient
from custom_components.dahua.light import DahuaSecurityLight


def _coordinator(channel: int = 0) -> DahuaDataUpdateCoordinator:
    coordinator = object.__new__(DahuaDataUpdateCoordinator)
    coordinator.model = "PTZ3E10X-T180"
    coordinator._channel = channel
    coordinator._channel_number = channel + 1
    coordinator._nvr_active_deterrence = False
    coordinator._device_class = "SD"
    coordinator._supports_rpc2_siren = False
    coordinator._supports_rpc2_security_light = False
    coordinator._manual_siren = False
    coordinator._manual_security_light = False
    return coordinator


def _light(coordinator: DahuaDataUpdateCoordinator) -> DahuaSecurityLight:
    light = object.__new__(DahuaSecurityLight)
    light._coordinator = coordinator
    light.coordinator = coordinator
    return light


def test_model_exposes_security_light():
    assert _coordinator().supports_security_light()


def test_standalone_entry_uses_distinct_control_and_status_channels():
    coordinator = _coordinator()

    assert not coordinator.is_nvr_channel()
    assert coordinator.get_security_light_control_channel() == 1
    assert coordinator.get_coaxial_status_channel() == 2
    assert coordinator.get_rpc2_coaxial_status_channel() == 1
    assert coordinator.get_security_light_off_io() == 0


def test_second_sensor_entry_does_not_duplicate_the_shared_direct_control():
    coordinator = _coordinator(channel=1)

    assert not coordinator.uses_recorder_deterrence()
    assert not coordinator.creates_security_light_entity()
    assert coordinator.get_security_light_control_channel() == 1
    assert coordinator.get_coaxial_status_channel() == 2
    assert coordinator.get_rpc2_coaxial_status_channel() == 1
    assert coordinator.get_security_light_off_io() == 0


async def test_client_can_use_model_specific_zero_to_disable():
    client = DahuaClient("u", "p", "camera", 80, 554, None)
    client.get = AsyncMock(return_value={})

    await client.async_set_coaxial_control_state(1, SECURITY_LIGHT_TYPE, False, 0)

    expected = (
        "/cgi-bin/coaxialControlIO.cgi?action=control&channel=1"
        "&info[0].Type=1&info[0].IO=0"
    )
    assert client.get.await_args.args == (expected,)


async def test_security_light_uses_the_verified_t180_mapping():
    coordinator = _coordinator()
    coordinator.client = AsyncMock()
    coordinator.async_refresh = AsyncMock()
    light = _light(coordinator)

    await light.async_turn_on()
    await light.async_turn_off()

    assert coordinator.client.async_set_coaxial_control_state.await_args_list == [
        ((1, SECURITY_LIGHT_TYPE, True),),
        ((1, SECURITY_LIGHT_TYPE, False, 0),),
    ]
    assert coordinator.async_refresh.await_count == 2


async def test_rpc2_security_light_uses_t180_status_and_off_mapping():
    coordinator = _coordinator()
    coordinator._supports_rpc2_security_light = True
    coordinator._supports_rpc2_siren = False
    coordinator.client = AsyncMock()
    coordinator.async_refresh = AsyncMock()
    light = _light(coordinator)

    await light.async_turn_on()
    await light.async_turn_off()

    assert coordinator.client.async_set_coaxial_control_state_rpc2.await_args_list == [
        ((SECURITY_LIGHT_TYPE, True),),
        ((SECURITY_LIGHT_TYPE, False, 0),),
    ]
    assert coordinator.async_refresh.await_count == 2


async def test_client_reads_model_selected_rpc2_status_channel():
    status = type("Status", (), {"speaker": False, "white_light": True})()
    rpc = AsyncMock()
    rpc.get_coaxial_control_io_status.return_value = status
    client = DahuaClient("u", "p", "camera", 80, 554, None)
    client._shared_rpc2 = AsyncMock(return_value=type("Holder", (), {"client": rpc})())

    result = await client.async_get_coaxial_control_io_status_rpc2(1)

    rpc.get_coaxial_control_io_status.assert_awaited_once_with(1)
    assert result["status.WhiteLight"] == "On"


async def test_refusal_safe_poll_wrapper_uses_t180_rpc2_status_channel():
    coordinator = _coordinator()
    coordinator._supports_rpc2_security_light = True
    coordinator.client = AsyncMock()
    coordinator.client.async_get_coaxial_control_io_status_rpc2.return_value = {
        "status.WhiteLight": "Off"
    }

    result = await coordinator._async_coaxial_status(
        coordinator.get_rpc2_coaxial_status_channel()
    )

    assert result == {"status.WhiteLight": "Off"}
    coordinator.client.async_get_coaxial_control_io_status_rpc2.assert_awaited_once_with(
        1
    )


async def test_client_can_use_model_specific_rpc2_zero_to_disable():
    rpc = AsyncMock()
    client = DahuaClient("u", "p", "camera", 80, 554, None)
    client._shared_rpc2 = AsyncMock(return_value=type("Holder", (), {"client": rpc})())

    await client.async_set_coaxial_control_state_rpc2(SECURITY_LIGHT_TYPE, False, 0)

    rpc.set_coaxial_control_state.assert_awaited_once_with(
        0, SECURITY_LIGHT_TYPE, False, 0
    )


async def test_existing_cameras_keep_the_default_off_value():
    client = DahuaClient("u", "p", "camera", 80, 554, None)
    client.get = AsyncMock(return_value={})

    await client.async_set_coaxial_control_state(0, SECURITY_LIGHT_TYPE, False)

    expected = (
        "/cgi-bin/coaxialControlIO.cgi?action=control&channel=0"
        "&info[0].Type=1&info[0].IO=2"
    )
    assert client.get.await_args.args == (expected,)
