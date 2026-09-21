"""Direct IPC capability discovery must not change recorder behavior."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.dahua import DahuaDataUpdateCoordinator
from custom_components.dahua.client import DahuaClient
from custom_components.dahua.light import DahuaSecurityLight
from custom_components.dahua.rpc2 import DahuaRpc2Client
from custom_components.dahua.switch import DahuaSirenBinarySwitch
from tests.dahua.test_probe_timeouts import _coordinator, _Client


def coordinator(model="OEM-IPC", speaker=False, light=False, nvr=False):
    c = object.__new__(DahuaDataUpdateCoordinator)
    c.model = model
    c._channel = 0
    c._channel_number = 1
    c._nvr_active_deterrence = nvr
    c._supports_rpc2_siren = speaker
    c._supports_rpc2_security_light = light
    c.client = SimpleNamespace(
        async_get_coaxial_control_io_caps_rpc2=AsyncMock(),
        async_set_coaxial_control_state_rpc2=AsyncMock(),
        async_set_coaxial_control_state=AsyncMock(),
        async_set_nvr_coaxial_control_state=AsyncMock(),
    )
    c.async_refresh = AsyncMock()
    return c


@pytest.mark.parametrize("speaker,light", [(1, 1), (0, 0), (1, 0), (0, 1)])
async def test_caps_parse_and_independent_entity_gates(speaker, light):
    rpc = DahuaRpc2Client("u", "p", "camera", 80, 554, None)
    rpc.request = AsyncMock(return_value={"result": True, "params": {"caps": {
        "SupportControlSpeaker": speaker, "SupportControlLight": light,
    }}})
    caps = await rpc.get_coaxial_control_io_caps()
    rpc.request.assert_awaited_once_with(method="CoaxialControlIO.getCaps", params={"channel": 0})
    c = coordinator()
    c.client.async_get_coaxial_control_io_caps_rpc2.return_value = caps
    await c._async_probe_direct_deterrence()
    assert c.supports_siren() is bool(speaker)
    assert c.supports_security_light() is bool(light)
    assert c.uses_rpc2_deterrence(2) is bool(speaker)
    assert c.uses_rpc2_deterrence(1) is bool(light)


@pytest.mark.parametrize("error", [TimeoutError(), ConnectionError(), ValueError(), KeyError()])
@pytest.mark.parametrize("model,expected", [("IPC-HDW3849HP-AS-PV", True), ("OEM-IPC", False)])
async def test_probe_failure_keeps_model_fallback(error, model, expected):
    c = coordinator(model)
    c.client.async_get_coaxial_control_io_caps_rpc2.side_effect = error
    await c._async_probe_direct_deterrence()
    assert c.supports_siren() is expected
    assert c.supports_security_light() is expected
    assert not c.uses_rpc2_deterrence()


@pytest.mark.parametrize("model", ["TPC-BF1241-TB3F4-DW-S8-HW", "TPC-BF1241", "TPC-BF1241-OTHER"])
async def test_thermal_zero_speaker_keeps_family_fallback(model):
    c = coordinator(model)
    c.client.async_get_coaxial_control_io_caps_rpc2.return_value = {
        "SupportControlSpeaker": False, "SupportControlLight": True,
    }
    await c._async_probe_direct_deterrence()
    assert c.supports_siren()
    assert c.supports_security_light()
    assert not c.uses_rpc2_deterrence(2)
    assert not coordinator("TPC-OTHER").supports_siren()


@pytest.mark.parametrize("model,nvr", [("NVR5216", False), ("OEM-recorder", True)])
async def test_nvr_channel_zero_never_probes_rpc2(model, nvr):
    c = coordinator(model, nvr=nvr)
    await c._async_probe_direct_deterrence()
    c.client.async_get_coaxial_control_io_caps_rpc2.assert_not_awaited()
    assert not c.uses_rpc2_deterrence()


@pytest.mark.parametrize("on", [True, False])
@pytest.mark.parametrize("entity_class,kind", [(DahuaSirenBinarySwitch, 2), (DahuaSecurityLight, 1)])
@pytest.mark.parametrize("transport", ["rpc2", "legacy", "nvr"])
async def test_entity_control_transport(entity_class, kind, on, transport):
    c = coordinator(speaker=True, light=True, nvr=transport == "nvr")
    if transport == "legacy":
        c._supports_rpc2_siren = c._supports_rpc2_security_light = False
    entity = object.__new__(entity_class)
    entity._coordinator = c
    await (entity.async_turn_on() if on else entity.async_turn_off())
    if transport == "rpc2":
        c.client.async_set_coaxial_control_state_rpc2.assert_awaited_once_with(kind, on)
        c.client.async_set_coaxial_control_state.assert_not_awaited()
        c.client.async_set_nvr_coaxial_control_state.assert_not_awaited()
    elif transport == "legacy":
        c.client.async_set_coaxial_control_state.assert_awaited_once_with(0, kind, on)
        c.client.async_set_coaxial_control_state_rpc2.assert_not_awaited()
    else:
        c.client.async_set_nvr_coaxial_control_state.assert_awaited_once_with(1, kind, on)
        c.client.async_set_coaxial_control_state_rpc2.assert_not_awaited()


@pytest.mark.parametrize("on", [True, False])
@pytest.mark.parametrize("kind", [1, 2])
async def test_rpc2_control_payload(kind, on):
    rpc = DahuaRpc2Client("u", "p", "camera", 80, 554, None)
    rpc.request = AsyncMock(return_value={"result": True})
    await rpc.set_coaxial_control_state(0, kind, on)
    rpc.request.assert_awaited_once_with(method="CoaxialControlIO.control", params={
        "channel": 0, "info": [{"Type": kind, "IO": 1 if on else 2, "TriggerMode": 2}],
    })


@pytest.mark.parametrize("speaker,light", [("On", "Off"), ("Off", "On"), ("Off", "Off")])
async def test_status_parsed_and_flattened(speaker, light):
    rpc = DahuaRpc2Client("u", "p", "camera", 80, 554, None)
    rpc.request = AsyncMock(return_value={"result": True, "params": {"status": {
        "Speaker": speaker, "WhiteLight": light,
    }}})
    client = DahuaClient("u", "p", "camera", 80, 554, None)
    client._shared_rpc2 = AsyncMock(return_value=SimpleNamespace(client=rpc))
    assert await client.async_get_coaxial_control_io_status_rpc2() == {
        "status.Speaker": speaker, "status.WhiteLight": light,
    }
    rpc.request.assert_awaited_once_with(method="CoaxialControlIO.getStatus", params={"channel": 0})


@pytest.mark.parametrize("caps", [{}, {"SupportControlSpeaker": "0", "SupportControlLight": "1"}])
async def test_missing_and_string_capabilities(caps):
    rpc = DahuaRpc2Client("u", "p", "camera", 80, 554, None)
    rpc.request = AsyncMock(return_value={"params": {"caps": caps}})
    result = await rpc.get_coaxial_control_io_caps()
    assert result["SupportControlSpeaker"] is False
    assert result["SupportControlLight"] is bool(caps)


@pytest.mark.parametrize("params", [None, {}, {"caps": []}])
async def test_malformed_caps_rejected(params):
    rpc = DahuaRpc2Client("u", "p", "camera", 80, 554, None)
    rpc.request = AsyncMock(return_value={"params": params})
    with pytest.raises(ValueError):
        await rpc.get_coaxial_control_io_caps()


@pytest.mark.parametrize("speaker,light", [(True, True), (False, False), (True, False)])
async def test_setup_caches_caps_and_poll_uses_selected_status(speaker, light):
    client = _Client()
    client.use_rpc2 = False
    client.async_get_coaxial_control_io_caps_rpc2 = AsyncMock(return_value={
        "SupportControlSpeaker": speaker, "SupportControlLight": light,
    })
    client.async_get_coaxial_control_io_status_rpc2 = AsyncMock(return_value={
        "status.Speaker": "On", "status.WhiteLight": "Off",
    })
    c = _coordinator(client)
    first = await c._async_update_data()
    await c._async_update_data()
    client.async_get_coaxial_control_io_caps_rpc2.assert_awaited_once()
    if speaker or light:
        assert client.async_get_coaxial_control_io_status_rpc2.await_count == 2
        assert "async_get_coaxial_control_io_status" not in client.called
        assert first["status.Speaker"] == "On"
    else:
        client.async_get_coaxial_control_io_status_rpc2.assert_not_awaited()
        assert not c.supports_siren()
        assert not c.supports_security_light()


async def test_rpc2_security_light_skips_lighting_v2_fallback():
    client = _Client()
    client.use_rpc2 = False
    client.async_get_coaxial_control_io_caps_rpc2 = AsyncMock(return_value={
        "SupportControlSpeaker": False, "SupportControlLight": True,
    })
    client.async_get_coaxial_control_io_status_rpc2 = AsyncMock(return_value={
        "status.Speaker": "Off", "status.WhiteLight": "On",
    })
    client.async_get_lighting_v2 = AsyncMock(side_effect=TimeoutError)
    c = _coordinator(client)

    for poll_count in (1, 2):
        data = await c._async_update_data()
        assert c.initialized
        assert c.supports_security_light()
        assert c.uses_rpc2_deterrence(1)
        assert c._supports_lighting_v2 is False
        assert data["status.WhiteLight"] == "On"
        assert client.async_get_coaxial_control_io_status_rpc2.await_count == poll_count
        # The failed initial capability probe must be the only Lighting_V2 read.
        client.async_get_lighting_v2.assert_awaited_once_with()
    client.async_get_coaxial_control_io_caps_rpc2.assert_awaited_once_with()


async def test_client_caps_and_control_fix_channel_zero():
    rpc = SimpleNamespace(get_coaxial_control_io_caps=AsyncMock(return_value={}),
                          set_coaxial_control_state=AsyncMock())
    client = DahuaClient("u", "p", "camera", 80, 554, None)
    client._shared_rpc2 = AsyncMock(return_value=SimpleNamespace(client=rpc))
    await client.async_get_coaxial_control_io_caps_rpc2()
    await client.async_set_coaxial_control_state_rpc2(2, True)
    rpc.get_coaxial_control_io_caps.assert_awaited_once_with(0)
    rpc.set_coaxial_control_state.assert_awaited_once_with(0, 2, True)
