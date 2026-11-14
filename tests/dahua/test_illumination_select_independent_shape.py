"""#1204: nine scheme scenes need not match three Lighting_V2 profiles."""

from copy import deepcopy
from unittest.mock import AsyncMock, Mock

import pytest

from custom_components.dahua import coordinator as coordinator_module
from custom_components.dahua.client import DahuaClient
from custom_components.dahua.coordinator import DahuaDataUpdateCoordinator
from custom_components.dahua.select import DahuaLightingSchemeSelect
from tests.dahua.test_what_setup_probes import _coordinator

MODES = ["InfraredMode", "WhiteMode", "AIMode"]


def _scenes(count=9):
    return [
        {"LightingMode": "AIMode", "SchemeSchedule": {"Enable": True, "Keep": i}}
        for i in range(count)
    ]


def _lighting(count=3):
    return [
        [
            {"LightType": "InfraredLight", "Mode": "Auto"},
            {"LightType": "WhiteLight", "Mode": "Auto"},
            {"LightType": "AIMixLight", "Mode": "Auto"},
        ]
        for _ in range(count)
    ]


class _Device:
    def __init__(self, scheme, lighting, channel=0, modes=MODES):
        self.tables = {
            "LightingScheme": deepcopy(scheme),
            "Lighting_V2": deepcopy(lighting),
        }
        self.channel = channel
        self.modes = modes
        self.reads = []
        self.writes = []
        self.caps_reads = 0

    async def get_config(self, params):
        assert params == {
            "name": params["name"],
            "onlyLocal": False,
            "channel": self.channel,
        }
        self.reads.append(params["name"])
        return {"table": deepcopy(self.tables[params["name"]])}

    async def get_lighting_schemes(self, channel):
        assert channel == self.channel
        self.caps_reads += 1
        return self.modes

    async def set_config(self, name, table, *, channel):
        assert name == "LightingScheme"
        assert channel == self.channel
        self.writes.append((name, deepcopy(table), channel))
        self.tables[name] = deepcopy(table)


def _client(device):
    client = DahuaClient("u", "p", "shape-review-camera", 80, 554, object())

    async def call(operation):
        return await operation(device)

    client._rpc2_shared_call = call
    return client


def _setup(hass, device):
    coordinator = _coordinator(hass)
    coordinator._channel = device.channel
    client = _client(device)
    for name in (
        "async_channel_scoped_white_light_supported",
        "async_get_channel_lighting_scheme",
        "async_get_lighting_schemes",
    ):
        setattr(coordinator.client, name, getattr(client, name))
    return coordinator


@pytest.mark.parametrize("channel", [0, 3])
async def test_mismatched_tables_still_create_a_working_mode_select(hass, channel):
    device = _Device(_scenes(), _lighting(), channel)
    coordinator = _setup(hass, device)
    coordinator.data = await coordinator._async_update_data()
    assert coordinator.initialized
    assert coordinator.supports_lighting_schemes()
    assert not coordinator.uses_channel_scoped_illuminator()
    entity = DahuaLightingSchemeSelect(coordinator, coordinator.config_entry)
    assert entity.options == ["infrared", "white", "smart_dual"]
    assert entity.current_option == "smart_dual"
    assert len([k for k in coordinator.data if k.endswith("].LightingMode")]) == 9
    assert device.caps_reads == 1


async def test_matching_tables_keep_the_existing_white_light_capability(hass):
    device = _Device(_scenes(3), _lighting())
    coordinator = _setup(hass, device)
    coordinator.data = await coordinator._async_update_data()
    assert coordinator.supports_lighting_schemes()
    assert coordinator.uses_channel_scoped_illuminator()
    assert device.reads.count("LightingScheme") == 2  # White-light probe and poll.


@pytest.mark.parametrize(
    "scheme", [[], [None], [{}], [{"LightingMode": 7}], [{"LightingMode": ""}]]
)
async def test_invalid_scheme_does_not_authorize_a_selector(hass, scheme):
    device = _Device(scheme, _lighting())
    coordinator = _setup(hass, device)
    await coordinator._async_update_data()
    assert coordinator.initialized
    assert not coordinator.supports_lighting_schemes()
    assert device.caps_reads == 0


async def test_the_current_mode_does_not_invent_a_capability_list(hass):
    device = _Device(_scenes(), _lighting(), modes=None)
    coordinator = _setup(hass, device)
    await coordinator._async_update_data()
    assert coordinator.initialized
    assert not coordinator.supports_lighting_schemes()


async def test_select_disabled_does_not_add_the_independent_probe(hass):
    coordinator = _coordinator(hass)
    coordinator._wanted_by = lambda *platforms: False
    coordinator.client.async_get_channel_lighting_scheme = AsyncMock()
    coordinator.client.async_get_lighting_schemes = AsyncMock()
    await coordinator._async_update_data()
    assert coordinator.initialized
    coordinator.client.async_get_channel_lighting_scheme.assert_not_awaited()
    coordinator.client.async_get_lighting_schemes.assert_not_awaited()


async def test_mode_write_preserves_all_scenes_and_never_writes_lighting_v2():
    device = _Device(_scenes(), _lighting(), channel=3)
    original = deepcopy(device.tables)
    client = _client(device)
    await client.async_set_channel_scoped_lighting_scheme(3, "WhiteMode")
    expected = deepcopy(original["LightingScheme"])
    for row in expected:
        row["LightingMode"] = "WhiteMode"
    assert device.writes == [("LightingScheme", expected, 3)]
    assert device.tables["LightingScheme"] == expected
    assert device.tables["Lighting_V2"] == original["Lighting_V2"]


async def test_an_unanswered_independent_probe_can_discover_the_selector_later(
    hass, monkeypatch
):
    device = _Device(_scenes(), _lighting())
    coordinator = _setup(hass, device)
    coordinator._supports_channel_scoped_illuminator = False
    coordinator._note_probe_refusal = (
        DahuaDataUpdateCoordinator._note_probe_refusal.__get__(coordinator)
    )
    read = coordinator.client.async_get_channel_lighting_scheme
    coordinator.client.async_get_channel_lighting_scheme = AsyncMock(
        side_effect=TimeoutError()
    )
    assert not await coordinator._async_probe_lighting_schemes()
    assert "lighting_schemes" in coordinator.probes_that_did_not_answer()
    coordinator.client.async_get_channel_lighting_scheme = read
    notify = Mock()
    monkeypatch.setattr(
        coordinator_module, "async_notify_capability_discovered", notify
    )
    await coordinator._async_retry_unanswered_capabilities()
    assert coordinator.supports_lighting_schemes()
    notify.assert_called_once_with(hass, "e1", coordinator.get_serial_number())
