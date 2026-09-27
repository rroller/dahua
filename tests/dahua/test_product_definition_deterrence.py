"""Independent deterrence evidence, multi-channel selection and diagnostics."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, call
import pytest
from custom_components.dahua.client import DahuaClient
from custom_components.dahua.deterrence import (
    product_definition_supports_security_light,
    product_definition_supports_siren,
)
from custom_components.dahua.rpc2 import DahuaRpc2Client
from custom_components.dahua.diagnostics import _capabilities_block
from tests.dahua.test_direct_rpc2_deterrence import coordinator

RED_BLUE = {
    "LinkingDetail": {
        "FilckerLighting": {"Support": True, "LightType": ["RedBlueLight"]}
    }
}
BF1241_AUDIO = {
    "SirenFileManager": {"Support": False},
    "SupportEventLinkList": ["FireWarning"],
    "PlayFormet": ["wav"],
}
SIREN = {"SirenFileManager": {"Support": True}}


@pytest.mark.parametrize("key", ["FilckerLighting", "FlickerLighting"])
@pytest.mark.parametrize(
    "types", [["WhiteLight"], ["RedBlueLight"], ["OtherLight"], [""]]
)
def test_supported_linked_light_requires_only_nonempty_list(key, types):
    assert product_definition_supports_security_light(
        {"LinkingDetail": {key: {"Support": True, "LightType": types}}}
    )


@pytest.mark.parametrize(
    "definition",
    [
        None,
        {},
        [],
        "light",
        {"LinkingDetail": []},
        {"WhiteLight": {"Support": True}},
        {
            "LinkingDetail": {
                "KeepLighting": {"Support": True, "LightType": ["WhiteLight"]}
            }
        },
    ],
)
def test_missing_linked_light_is_negative(definition):
    assert not product_definition_supports_security_light(definition)


@pytest.mark.parametrize(
    "support,types",
    [
        (False, ["RedBlueLight"]),
        ("true", ["RedBlueLight"]),
        (1, ["RedBlueLight"]),
        (True, []),
        (True, None),
        (True, "RedBlueLight"),
    ],
)
def test_false_empty_or_malformed_lighting(support, types):
    assert not product_definition_supports_security_light(
        {"LinkingDetail": {"FilckerLighting": {"Support": support, "LightType": types}}}
    )


def test_alternate_spelling_not_masked_by_negative():
    assert product_definition_supports_security_light(
        {
            "LinkingDetail": {
                "FilckerLighting": {"Support": False},
                "FlickerLighting": {"Support": True, "LightType": ["Other"]},
            }
        }
    )


@pytest.mark.parametrize("key", ["PlayFormat", "PlayFormet"])
@pytest.mark.parametrize("fmt", ["wav", "pcm", "aac", "mp3", "WAV"])
def test_event_audio_formats_without_other_requirements(key, fmt):
    assert product_definition_supports_siren(
        {
            "SupportEventLinkList": ["AlarmLocal"],
            key: [fmt],
            "Support": False,
            "VoiceLinkFileOptional": False,
        }
    )


@pytest.mark.parametrize(
    "definition",
    [
        SIREN,
        BF1241_AUDIO,
        {
            "SupportEventLinkList": ["AlarmLocal"],
            "PlayFormat": ["unknown"],
            "PlayFormet": ["mp3"],
        },
    ],
)
def test_explicit_and_event_audio_siren(definition):
    assert product_definition_supports_siren(definition)


@pytest.mark.parametrize(
    "definition",
    [
        None,
        {},
        [],
        {"SupportRecordAudio": True},
        {"SirenFileManager": {"Support": "true"}},
        {"PlayFormat": ["wav"]},
        {"SupportEventLinkList": [], "PlayFormat": ["wav"]},
        {"SupportEventLinkList": [None], "PlayFormat": ["wav"]},
        {"SupportEventLinkList": [""], "PlayFormat": ["wav"]},
        {"SupportEventLinkList": ["AlarmLocal"], "PlayFormat": "wav"},
        {"SupportEventLinkList": ["AlarmLocal"], "PlayFormat": ["opus"]},
    ],
)
def test_audio_alone_or_missing_required_evidence_not_siren(definition):
    assert not product_definition_supports_siren(definition)


async def test_full_definition_one_call_plus_independent_caps_and_sources():
    c = coordinator()
    c.client.async_get_product_definition_rpc2.return_value = {
        "LightingControl": RED_BLUE,
        "AudioFileManager": SIREN,
    }
    c.client.async_get_coaxial_control_io_caps_rpc2.return_value = {
        "SupportControlSpeaker": True,
        "SupportControlLight": True,
    }
    await c._async_probe_direct_deterrence()
    c.client.async_get_product_definition_rpc2.assert_awaited_once_with()
    c.client.async_get_coaxial_control_io_caps_rpc2.assert_awaited_once_with()
    assert c.supports_siren() and c.supports_security_light()
    assert len(c.get_siren_detection_sources()) == 2
    assert len(c.get_security_light_detection_sources()) == 2
    assert all("false" not in s for s in c.get_siren_detection_sources())


@pytest.mark.parametrize("channel", [0, 1])
async def test_multichannel_camera_detects_and_preserves_channel_control(channel):
    c = coordinator()
    c._channel = channel
    c._channel_number = channel + 1
    c._device_class = "SD"
    c.client.async_get_product_definition_rpc2.return_value = {
        "LightingControlMulti": [RED_BLUE, RED_BLUE],
        "AudioFileManager": SIREN,
    }
    await c._async_probe_direct_deterrence()
    assert c.supports_siren() and c.supports_security_light()
    assert any(
        f"LightingControlMulti[{channel}]" in s
        for s in c.get_security_light_detection_sources()
    )
    if channel:
        from custom_components.dahua.light import DahuaSecurityLight
        from custom_components.dahua.switch import DahuaSirenBinarySwitch

        for cls, kind in [(DahuaSecurityLight, 1), (DahuaSirenBinarySwitch, 2)]:
            e = object.__new__(cls)
            e._coordinator = c
            await e.async_turn_on()
            c.client.async_set_nvr_coaxial_control_state.assert_awaited_with(
                channel + 1, kind, True
            )
        c.client.async_set_coaxial_control_state_rpc2.assert_not_awaited()


async def test_negative_single_definition_still_checks_multi():
    c = coordinator()
    c._channel = 1
    c.client.async_get_product_definition_rpc2.return_value = {
        "LightingControl": {},
        "LightingControlMulti": [{}, RED_BLUE],
        "AudioFileManager": {},
    }
    await c._async_probe_direct_deterrence()
    assert c.supports_security_light() and not c.supports_siren()


@pytest.mark.parametrize("channel", [-1, 1, 2])
async def test_multi_never_borrows_another_channel(channel):
    c = coordinator()
    c._channel = channel

    async def read(name=None):
        return (
            {
                "LightingControl": {},
                "LightingControlMulti": [RED_BLUE, {}],
                "AudioFileManager": {},
            }
            if name is None
            else None
        )

    c.client.async_get_product_definition_rpc2.side_effect = read
    await c._async_probe_direct_deterrence()
    assert not c.supports_security_light()
    reasons = c.get_security_light_detection_sources()
    assert any("Light" in s for s in reasons)
    if channel in (-1, 2):
        assert any("out of range" in s for s in reasons)


async def test_named_fallback_after_full_failure():
    c = coordinator()

    async def read(name=None):
        if name is None:
            raise TimeoutError
        return RED_BLUE if name == "LightingControl" else SIREN

    c.client.async_get_product_definition_rpc2.side_effect = read
    await c._async_probe_direct_deterrence()
    assert c.supports_siren() and c.supports_security_light()
    assert c.client.async_get_product_definition_rpc2.await_args_list == [
        call(),
        call("LightingControl"),
        call("AudioFileManager"),
    ]


@pytest.mark.parametrize(
    "pd_positive,caps_positive",
    [(True, False), (False, True), (True, True), (False, False)],
)
async def test_independent_positive_evidence_or(pd_positive, caps_positive):
    c = coordinator()
    c.client.async_get_product_definition_rpc2.return_value = {
        "LightingControl": RED_BLUE if pd_positive else {},
        "AudioFileManager": SIREN if pd_positive else {},
        "LightingControlMulti": [],
    }
    c.client.async_get_coaxial_control_io_caps_rpc2.return_value = {
        "SupportControlSpeaker": caps_positive,
        "SupportControlLight": caps_positive,
    }
    await c._async_probe_direct_deterrence()
    assert c.supports_siren() is (pd_positive or caps_positive)
    assert c.supports_security_light() is (pd_positive or caps_positive)


@pytest.mark.parametrize("failed", ["pd", "caps"])
async def test_probe_error_never_removes_other_positive(failed):
    c = coordinator()
    c.client.async_get_product_definition_rpc2.return_value = {
        "LightingControl": RED_BLUE,
        "AudioFileManager": BF1241_AUDIO,
    }
    c.client.async_get_coaxial_control_io_caps_rpc2.return_value = {
        "SupportControlSpeaker": True,
        "SupportControlLight": True,
    }
    if failed == "pd":
        c.client.async_get_product_definition_rpc2.side_effect = ConnectionError
    else:
        c.client.async_get_coaxial_control_io_caps_rpc2.side_effect = TimeoutError
    await c._async_probe_direct_deterrence()
    assert c.supports_siren() and c.supports_security_light()


@pytest.mark.parametrize("device_class", ["NVR", "DVR", "XVR", "HCVR", " nvr "])
async def test_recorder_never_probes_direct_capabilities(device_class):
    c = coordinator()
    c._device_class = device_class
    await c._async_probe_direct_deterrence()
    c.client.async_get_product_definition_rpc2.assert_not_awaited()
    c.client.async_get_coaxial_control_io_caps_rpc2.assert_not_awaited()
    assert any("recorder" in s for s in c.get_siren_detection_sources())


@pytest.mark.parametrize("siren,light", [(True, False), (False, True), (True, True)])
async def test_multichannel_manual_overrides_without_probe_support(siren, light):
    c = coordinator(manual_siren=siren, manual_light=light)
    c._channel = 1
    c.client.async_get_product_definition_rpc2.side_effect = TimeoutError
    c.client.async_get_coaxial_control_io_caps_rpc2.side_effect = TimeoutError
    await c._async_probe_direct_deterrence()
    assert c.supports_siren() is siren
    assert c.supports_security_light() is light
    assert not c.uses_rpc2_deterrence()
    if siren:
        assert c.get_siren_detection_sources() == ["Manual override: manual_siren=true"]
    if light:
        assert c.get_security_light_detection_sources() == [
            "Manual override: manual_security_light=true"
        ]


async def test_negative_diagnostics_explain_failed_rules_without_extra_calls():
    c = coordinator()
    c.client.async_get_product_definition_rpc2.return_value = {
        "LightingControl": {
            "LinkingDetail": {"FilckerLighting": {"Support": True, "LightType": []}}
        },
        "LightingControlMulti": [],
        "AudioFileManager": {"SupportEventLinkList": [], "PlayFormat": ["opus"]},
    }
    c.client.async_get_coaxial_control_io_caps_rpc2.return_value = {
        "SupportControlSpeaker": False,
        "SupportControlLight": False,
    }
    await c._async_probe_direct_deterrence()
    counts = (
        c.client.async_get_product_definition_rpc2.await_count,
        c.client.async_get_coaxial_control_io_caps_rpc2.await_count,
    )
    siren = c.get_siren_detection_sources()
    light = c.get_security_light_detection_sources()
    assert any("SupportEventLinkList" in s for s in siren)
    assert any("no supported" in s for s in siren)
    assert any("LightType" in s and "empty" in s for s in light)
    assert any("SupportControlLight" in s and "not true" in s for s in light)
    assert "Manual override: disabled" in siren
    assert counts == (
        c.client.async_get_product_definition_rpc2.await_count,
        c.client.async_get_coaxial_control_io_caps_rpc2.await_count,
    )


async def test_negative_errors_exclude_exception_messages_and_reset_old_evidence():
    c = coordinator()
    c.client.async_get_product_definition_rpc2.return_value = {
        "LightingControl": RED_BLUE,
        "AudioFileManager": SIREN,
    }
    await c._async_probe_direct_deterrence()
    c.client.async_get_product_definition_rpc2.side_effect = TimeoutError(
        "secret-password"
    )
    c.client.async_get_coaxial_control_io_caps_rpc2.side_effect = ConnectionError(
        "secret-password"
    )
    await c._async_probe_direct_deterrence()
    assert not c.supports_siren() and not c.supports_security_light()
    reasons = c.get_siren_detection_sources() + c.get_security_light_detection_sources()
    assert any("query failed (TimeoutError)" in s for s in reasons)
    assert any("query failed (ConnectionError)" in s for s in reasons)
    assert "secret-password" not in str(reasons)


@pytest.mark.parametrize(
    "name,definition",
    [
        (None, {"AudioFileManager": SIREN}),
        ("AudioFileManager", SIREN),
        ("LightingControlMulti", [RED_BLUE, {}]),
    ],
)
async def test_rpc2_definition_shapes_and_shared_session(name, definition):
    from custom_components.dahua.rpc2 import _PARAMS_UNSET

    rpc = DahuaRpc2Client("u", "p", "camera", 80, 554, None)
    rpc.request = AsyncMock(
        return_value={"result": True, "params": {"definition": definition}}
    )
    client = DahuaClient("u", "p", "camera", 80, 554, None)
    client._shared_rpc2 = AsyncMock(return_value=SimpleNamespace(client=rpc))
    assert await client.async_get_product_definition_rpc2(name) == definition
    rpc.request.assert_awaited_once_with(
        method="magicBox.getProductDefinition",
        params={"name": name} if name is not None else _PARAMS_UNSET,
        verify_result=False,
    )


async def test_bare_rpc_request_actually_omits_params():
    import json

    rpc = DahuaRpc2Client(
        "u",
        "p",
        "camera",
        80,
        554,
        SimpleNamespace(
            post=AsyncMock(
                return_value=SimpleNamespace(
                    text=AsyncMock(
                        return_value=json.dumps(
                            {"result": True, "params": {"definition": {}}}
                        )
                    )
                )
            )
        ),
    )
    assert await rpc.get_product_definition() == {}
    payload = rpc._session.post.await_args.kwargs["json"]
    assert "params" not in payload


@pytest.mark.parametrize(
    "response",
    [
        None,
        [],
        {},
        {"result": False, "params": {"definition": SIREN}},
        {"result": 1, "params": {"definition": SIREN}},
        {"result": True, "params": None},
        {"result": True, "params": {"definition": []}},
    ],
)
async def test_invalid_named_definition_not_positive(response):
    rpc = DahuaRpc2Client("u", "p", "camera", 80, 554, None)
    rpc.request = AsyncMock(return_value=response)
    assert await rpc.get_product_definition("AudioFileManager") is None


@pytest.mark.parametrize("supported", [True, False])
async def test_diagnostics_adjacent_sources_match_capability_and_make_no_requests(
    supported,
):
    c = coordinator()
    c.client.async_get_product_definition_rpc2.return_value = {
        "LightingControl": RED_BLUE if supported else {},
        "LightingControlMulti": [],
        "AudioFileManager": SIREN if supported else {},
    }
    await c._async_probe_direct_deterrence()
    before = (
        c.client.async_get_product_definition_rpc2.await_count,
        c.client.async_get_coaxial_control_io_caps_rpc2.await_count,
    )
    derived = _capabilities_block(c)["derived_from_model"]
    for name in ("siren", "security_light"):
        assert derived["supports_" + name] is supported
        assert derived["supports_" + name + "_sources"]
        if supported:
            assert any(
                "ProductDefinition" in r
                for r in derived["supports_" + name + "_sources"]
            )
        else:
            assert (
                "Manual override: disabled" in derived["supports_" + name + "_sources"]
            )
    assert before == (
        c.client.async_get_product_definition_rpc2.await_count,
        c.client.async_get_coaxial_control_io_caps_rpc2.await_count,
    )


@pytest.mark.parametrize("platform", ["switch", "light"])
@pytest.mark.parametrize(
    "device_class,channel,opt_in,expected",
    [("SD", 1, False, True), ("NVR", 0, False, False), ("NVR", 0, True, True)],
)
async def test_entity_creation_uses_host_class_not_channel(
    monkeypatch, platform, device_class, channel, opt_in, expected
):
    from custom_components.dahua import switch, light

    module = switch if platform == "switch" else light
    c = coordinator(speaker=True, light=True, nvr=opt_in)
    c._device_class = device_class
    c._channel = channel
    for name in (
        "supports_infrared_light",
        "supports_illuminator",
        "is_flood_light",
        "supports_smart_motion_detection",
        "supports_smart_motion_detection_amcrest",
        "supports_privacy_mode",
        "supports_alarm_output",
        "supports_disarming_linkage",
    ):
        setattr(c, name, lambda: False)
    monkeypatch.setattr(
        module,
        "DahuaSirenBinarySwitch" if platform == "switch" else "DahuaSecurityLight",
        lambda *args, **kwargs: "deterrence",
    )
    if platform == "switch":
        monkeypatch.setattr(
            module, "DahuaMotionDetectionBinarySwitch", lambda *args: "motion"
        )
    added = []
    await module.async_setup_entry(
        SimpleNamespace(data={"dahua": {"entry": c}}),
        SimpleNamespace(entry_id="entry"),
        added.extend,
    )
    assert ("deterrence" in added) is expected


@pytest.mark.parametrize("device_class", [None, {}, 0, ""])
async def test_missing_or_malformed_class_does_not_crash(device_class):
    c = coordinator()
    c._device_class = device_class
    c.client.async_get_coaxial_control_io_caps_rpc2.return_value = {
        "SupportControlSpeaker": True
    }
    await c._async_probe_direct_deterrence()
    assert c.supports_siren()


@pytest.mark.parametrize(
    "model",
    [
        "IPC-AS-PV",
        "IPC-L46N",
        "W452ASD",
        "AD410",
        "DB61I",
        "IP8M-2796E",
        "IPC-COLOR4M-TZ",
    ],
)
async def test_model_provenance_matches_existing_fallbacks(model):
    c = coordinator(model)
    await c._async_probe_direct_deterrence()
    for supported, reasons in [
        (c.supports_siren(), c.get_siren_detection_sources()),
        (c.supports_security_light(), c.get_security_light_detection_sources()),
    ]:
        if supported:
            assert any(r.startswith("Model fallback:") for r in reasons)
