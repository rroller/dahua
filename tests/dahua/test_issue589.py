"""Tests for SDT4E425 dual-sensor and RPC2 preset support."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from custom_components.dahua import camera as camera_platform
from custom_components.dahua import select as select_platform
from custom_components.dahua.client import DahuaClient
from custom_components.dahua.const import DOMAIN
from custom_components.dahua.model_profiles import is_sdt4e425
from custom_components.dahua.rpc2 import DahuaRpc2Client


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    async def text(self):
        return json.dumps(self._payload)


class _FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def post(self, url, json):
        self.calls.append((url, json))
        return _FakeResponse(self.responses.pop(0))


class _FakePlatform:
    def async_register_entity_service(self, *args, **kwargs):
        return None


class _FakeClient:
    @staticmethod
    def to_stream_name(subtype):
        return ("Main", "Sub", "Sub_2")[subtype]


class _FakeCoordinator:
    def __init__(self, model="DH-SDT4E425-4F-GB-A-PV1"):
        self.client = _FakeClient()
        self._model = model

    def get_model(self):
        return self._model

    def get_max_streams(self):
        return 3


class _CapturedCamera:
    def __init__(
        self,
        coordinator,
        stream_index,
        config_entry,
        *,
        logical_channel=None,
        media_channel=None,
        display_name=None,
        unique_suffix=None,
    ):
        self.stream_index = stream_index
        self.logical_channel = logical_channel
        self.media_channel = media_channel
        self.display_name = display_name
        self.unique_suffix = unique_suffix


def test_model_profile_is_narrow():
    assert is_sdt4e425("DH-SDT4E425-4F-GB-A-PV1")
    assert is_sdt4e425("SDT4E425-4F-GB-A-PV1")
    assert is_sdt4e425("DH-SDT4E425-4F-GB-A-PV1-S2")
    assert not is_sdt4e425("SDT4E425")
    assert not is_sdt4e425(None)


def test_sdt4e425_creates_two_sensors_with_three_streams(monkeypatch):
    monkeypatch.setattr(camera_platform, "DahuaCamera", _CapturedCamera)
    monkeypatch.setattr(
        camera_platform.entity_platform,
        "async_get_current_platform",
        lambda: _FakePlatform(),
    )

    coordinator = _FakeCoordinator()
    hass = SimpleNamespace(data={DOMAIN: {"entry": coordinator}})
    entry = SimpleNamespace(entry_id="entry", title="Camera")
    entities = []

    asyncio.run(camera_platform.async_setup_entry(hass, entry, entities.extend))

    assert [
        (
            entity.logical_channel,
            entity.media_channel,
            entity.stream_index,
            entity.display_name,
            entity.unique_suffix,
        )
        for entity in entities
    ] == [
        (0, 1, 0, "Panorama", "Main"),
        (0, 1, 1, "Panorama Sub", "Sub"),
        (0, 1, 2, "Panorama Sub_2", "Sub_2"),
        (1, 2, 0, "PTZ", "1_Main"),
        (1, 2, 1, "PTZ Sub", "1_Sub"),
        (1, 2, 2, "PTZ Sub_2", "1_Sub_2"),
    ]


def test_other_models_keep_native_stream_setup(monkeypatch):
    monkeypatch.setattr(camera_platform, "DahuaCamera", _CapturedCamera)
    monkeypatch.setattr(
        camera_platform.entity_platform,
        "async_get_current_platform",
        lambda: _FakePlatform(),
    )

    coordinator = _FakeCoordinator(model="OTHER")
    hass = SimpleNamespace(data={DOMAIN: {"entry": coordinator}})
    entry = SimpleNamespace(entry_id="entry", title="Camera")
    entities = []

    asyncio.run(camera_platform.async_setup_entry(hass, entry, entities.extend))

    assert len(entities) == 3
    assert [entity.stream_index for entity in entities] == [0, 1, 2]
    assert all(entity.logical_channel is None for entity in entities)
    assert all(entity.media_channel is None for entity in entities)


def test_rpc2_web5_login_promotes_authenticated_session():
    session = _FakeSession(
        [
            {
                "result": False,
                "session": "S1",
                "params": {
                    "realm": "realm",
                    "random": "random",
                    "encryption": "Default",
                },
            },
            {"result": True, "session": "S2", "params": {}},
        ]
    )
    client = DahuaRpc2Client("user", "password", "192.0.2.1", 80, 554, session)

    asyncio.run(client.login())

    assert client._session_id == "S2"
    assert session.calls[0][1]["params"]["clientType"] == "Web5.0"
    assert session.calls[1][1]["session"] == "S1"


def test_rpc2_get_presets_sends_explicit_null_params():
    session = _FakeSession(
        [
            {
                "result": True,
                "params": {"presets": [{"Index": 1}, {"Index": 5}]},
            }
        ]
    )
    client = DahuaRpc2Client("user", "password", "192.0.2.1", 80, 554, session)
    client._session_id = "S2"
    client._ptz_objects[1] = 42

    presets = asyncio.run(client.async_get_ptz_presets(1))

    assert [preset["Index"] for preset in presets] == [1, 5]
    payload = session.calls[0][1]
    assert payload["method"] == "ptz.getPresets"
    assert payload["object"] == 42
    assert "params" in payload
    assert payload["params"] is None


def test_rpc2_goto_preset_uses_observed_payload():
    session = _FakeSession([{"result": True, "params": {}}])
    client = DahuaRpc2Client("user", "password", "192.0.2.1", 80, 554, session)
    client._session_id = "S2"
    client._ptz_objects[1] = 42

    asyncio.run(client.async_goto_preset_position(1, 3))

    payload = session.calls[0][1]
    assert payload["method"] == "ptz.start"
    assert payload["object"] == 42
    assert payload["params"] == {
        "code": "GotoPreset",
        "arg1": 3,
        "arg2": 0,
        "arg3": 0,
    }


def test_parse_ptz_preset_ids_filters_and_sorts():
    assert DahuaClient.parse_ptz_preset_ids(
        [
            {"Index": "5"},
            {"Index": 1},
            {"Index": 5},
            {"Index": 0},
            {"Index": True},
            {"Index": "invalid"},
            {},
        ]
    ) == [1, 5]


def test_sdt4e425_select_uses_real_preset_ids(monkeypatch):
    monkeypatch.setattr(
        select_platform.DahuaBaseEntity,
        "__init__",
        lambda self, coordinator, config_entry: None,
    )
    coordinator = SimpleNamespace(
        client=SimpleNamespace(async_goto_preset_rpc2=AsyncMock()),
        async_refresh=AsyncMock(),
        get_device_name=lambda: "Camera",
        get_serial_number=lambda: "SERIAL",
    )

    entity = select_platform.DahuaCameraPresetPositionSelect(
        coordinator,
        SimpleNamespace(),
        preset_ids=[1, 3, 5],
        rpc2_channel=1,
    )

    assert entity.unique_id == "SERIAL_1_preset_position"
    assert entity.options == ["Manual", "1", "3", "5"]
    assert entity.current_option == "Manual"

    asyncio.run(entity.async_select_option("3"))

    coordinator.client.async_goto_preset_rpc2.assert_awaited_once_with(1, 3)
    coordinator.async_refresh.assert_awaited_once()
