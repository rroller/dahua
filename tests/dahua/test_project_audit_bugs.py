"""Regressions for channel recovery, event isolation, and late entity setup."""

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigSubentry

from custom_components import dahua
from custom_components.dahua import late
from custom_components.dahua.config_flow import CHANNEL_SUBENTRY
from custom_components.dahua.select import DahuaInfraredModeSelect
from tests.dahua.test_poll_skips_unused import _coordinator
from tests.dahua.test_setup_lifecycle import _entry, _no_platforms, _setting_up


async def test_a_channel_recovers_after_a_transient_startup_failure(hass):
    entry = _entry(hass)
    for channel in (0, 1):
        hass.config_entries.async_add_subentry(
            entry,
            ConfigSubentry(
                data={"channel": channel},
                subentry_type=CHANNEL_SUBENTRY,
                title=f"Channel {channel}",
                unique_id=f"audit-{channel}",
            ),
        )
    attempts = {0: 0, 1: 0}

    async def update(coordinator):
        channel = coordinator.get_channel()
        attempts[channel] += 1
        if channel == 1 and attempts[channel] == 1:
            raise TimeoutError("transient startup timeout")
        return {"serialNumber": "SER1"}

    starts, forward, unload = _no_platforms(hass)
    _setting_up(hass, entry)
    with patch.object(
        dahua.DahuaDataUpdateCoordinator, "_async_update_data", update
    ), starts, forward, unload, patch.object(
        dahua, "get_configured_scan_interval", return_value=timedelta(0)
    ), patch.object(
        hass.config_entries, "async_schedule_reload"
    ) as reload:
        assert await dahua.async_setup_entry(hass, entry)
        await asyncio.gather(*entry._background_tasks)
        reload.assert_called_once_with(entry.entry_id)
        assert attempts == {0: 1, 1: 2}, "healthy channels must not be probed again"
        await dahua.async_unload_entry(hass, entry)

        # The normal reload path now adopts both channels and creates their
        # entities through every platform's setup, without any pending retry.
        _setting_up(hass, entry)
        assert await dahua.async_setup_entry(hass, entry)
        assert set(entry.runtime_data) == {0, 1}
        assert not entry._background_tasks
        await dahua.async_unload_entry(hass, entry)


@pytest.mark.parametrize("during_request", [False, True])
async def test_startup_retries_are_cancelled_and_closed_on_unload(hass, during_request):
    entry = _entry(hass)
    entered_request = asyncio.Event()
    clients = []

    class RetryingCoordinator:
        last_update_success = False

        def __init__(self):
            self.closed = False
            clients.append(self)

        async def async_refresh(self):
            entered_request.set()
            await asyncio.Event().wait()

        async def async_stop(self):
            self.closed = True

    with patch.object(
        dahua,
        "get_configured_scan_interval",
        return_value=timedelta(seconds=0 if during_request else 3600),
    ), patch.object(hass.config_entries, "async_schedule_reload") as reload:
        entry.async_create_background_task(
            hass,
            dahua._async_retry_startup_channels(
                hass, entry, [(None, {})], lambda *args: RetryingCoordinator()
            ),
            "Retry Dahua startup channels",
        )
        if during_request:
            await entered_request.wait()
        await entry._async_process_on_unload(hass)
        assert not entry._background_tasks
        assert len(clients) == int(during_request)
        assert all(client.closed for client in clients)
        reload.assert_not_called()


async def test_failed_startup_retries_do_not_reload_healthy_channels(hass):
    entry = _entry(hass)
    attempts = 0
    clients = []
    tried_again = asyncio.Event()

    class UnavailableCoordinator:
        last_update_success = False

        def __init__(self):
            self.closed = False
            clients.append(self)

        async def async_refresh(self):
            nonlocal attempts
            attempts += 1
            if attempts == 3:
                tried_again.set()
                await asyncio.Event().wait()

        async def async_stop(self):
            self.closed = True

    with patch.object(
        dahua, "get_configured_scan_interval", return_value=timedelta(0)
    ), patch.object(hass.config_entries, "async_schedule_reload") as reload:
        task = entry.async_create_background_task(
            hass,
            dahua._async_retry_startup_channels(
                hass, entry, [(None, {})], lambda *args: UnavailableCoordinator()
            ),
            "Retry Dahua startup channels",
        )
        await tried_again.wait()
        assert not task.done()
        reload.assert_not_called()
        await entry._async_process_on_unload(hass)
        assert len(clients) == 3
        assert all(client.closed for client in clients)


@pytest.mark.parametrize("v2", [False, True])
@pytest.mark.parametrize("light_enabled", [False, True])
async def test_infrared_select_keeps_reading_when_light_platform_is_disabled(
    v2, light_enabled
):
    coordinator = _coordinator(light=light_enabled, select=True)
    coordinator._supports_lighting_v2 = v2
    coordinator.data = {}
    coordinator.client.async_get_config_lighting = AsyncMock(
        return_value={"table.Lighting[0][0].Mode": "Auto"}
    )
    coordinator.client.async_get_lighting_v2 = AsyncMock(
        return_value={
            "table.Lighting_V2[0][0][0].LightType": "InfraredLight",
            "table.Lighting_V2[0][0][0].Mode": "Auto",
        }
    )
    entity = object.__new__(DahuaInfraredModeSelect)
    entity._coordinator = coordinator
    assert coordinator.supports_infrared_light()
    coordinator.data = await coordinator._async_update_data()
    assert entity.current_option == "auto", coordinator.client.calls


async def test_capability_discovered_during_platform_setup_is_not_lost(hass):
    channels = [
        SimpleNamespace(
            subentry_id="s0", capable=False, get_serial_number=lambda: "S0"
        ),
        SimpleNamespace(
            subentry_id="s1", capable=False, get_serial_number=lambda: "S1"
        ),
    ]
    entry = SimpleNamespace(entry_id="audit", async_on_unload=lambda callback: None)
    second_channel_started = asyncio.Event()
    finish_second_channel = asyncio.Event()
    added = []

    async def build(coordinator):
        if coordinator is channels[1]:
            second_channel_started.set()
            await finish_second_channel.wait()
        return [SimpleNamespace(unique_id="S0_ir")] if coordinator.capable else []

    setup = asyncio.create_task(
        late.async_add_and_keep_watching(
            hass,
            entry,
            build,
            lambda entities, **kwargs: added.extend(entities),
            lambda entry: dict(enumerate(channels)),
        )
    )
    await second_channel_started.wait()
    channels[0].capable = True
    late.async_notify_capability_discovered(hass, entry.entry_id, "S0")
    await hass.async_block_till_done()
    finish_second_channel.set()
    await setup
    await hass.async_block_till_done()
    assert [e.unique_id for e in added] == ["S0_ir"]


async def test_event_stream_keys_include_normalized_http_ports(hass):
    with patch.dict(dahua._HOST_STREAMS, {}, clear=True):
        first = dahua._host_stream(hass, "10.0.0.1", 80)
        assert dahua._host_stream(hass, "10.0.0.1/", "80") is first
        assert dahua._host_stream(hass, "10.0.0.1", 8080) is not first


async def test_devices_on_different_ports_do_not_receive_each_others_events(hass):
    from tests.dahua.test_shared_event_stream import _Coordinator, ADDRESS, _settle

    first = _Coordinator(0, ["VideoMotion"])
    second = _Coordinator(0, ["VideoMotion"])
    for coordinator, port in ((first, 80), (second, 8080)):
        coordinator.hass = hass
        coordinator._address = ADDRESS
        coordinator._port = port
        coordinator.client.device_key = f"{ADDRESS}:{port}"
        coordinator.is_indoor_monitor_without_video = lambda: False

    with patch.dict(dahua._HOST_STREAMS, {}, clear=True):
        await dahua.DahuaDataUpdateCoordinator.async_start_event_listener(first)
        await dahua.DahuaDataUpdateCoordinator.async_start_event_listener(second)
        await _settle()
        first_stream = dahua._HOST_STREAMS[f"{ADDRESS}:80"]
        second_stream = dahua._HOST_STREAMS[f"{ADDRESS}:8080"]
        pending = [
            task
            for stream in (first_stream, second_stream)
            for task in (stream._task, stream._video_motion_poll_task)
            if task is not None
        ]
        try:
            first_stream.on_receive(b"Code=VideoMotion;action=Start;index=0\n", 0)
            assert first.handled
            assert second.handled == []
            second_stream.on_receive(b"Code=VideoMotion;action=Stop;index=0\n", 0)
            assert [event["action"] for event in first.handled] == ["Start"]
            assert [event["action"] for event in second.handled] == ["Stop"]
            assert first.client.attach_count == second.client.attach_count == 1

            from custom_components.dahua.diagnostics import _stream_block

            assert _stream_block(first)["this_entry_owns_it"] is True
            assert _stream_block(second)["this_entry_owns_it"] is True

            await dahua._release_host_stream(first)
            assert f"{ADDRESS}:80" not in dahua._HOST_STREAMS
            assert dahua._HOST_STREAMS[f"{ADDRESS}:8080"] is second_stream
            assert not second_stream._task.done()
        finally:
            await first_stream.async_stop()
            await second_stream.async_stop()
            await asyncio.gather(*pending, return_exceptions=True)


async def test_rpc2_motion_state_is_not_inherited_from_another_port(monkeypatch):
    from custom_components.dahua import client as client_module
    from custom_components.dahua.client import DahuaClient
    from custom_components.dahua.dahua_utils import parse_event
    from custom_components.dahua.diagnostics import _rpc2_poll_block
    from tests.dahua.test_rpc2_event_poll import (
        _client,
        _collect,
        _FakeRpc2Client,
        _StopPoll,
    )

    with patch.dict(client_module._HOST_RPC2_EVENT_STATE, {}, clear=True), patch.dict(
        client_module._HOST_RPC2_EVENT_POLL, {}, clear=True
    ):
        # Port 80 stays active across a stream restart. That retained state
        # must not suppress another camera's Start or invent its Stop.
        first = _client(monkeypatch, _FakeRpc2Client([{"VideoMotion": [0]}]))
        with pytest.raises(_StopPoll):
            await first._stream_events_rpc2(lambda *args: None, ["VideoMotion"], 0)

        _client(monkeypatch, _FakeRpc2Client([{"VideoMotion": [0]}]))
        second = DahuaClient("u", "p", "cam", 8080, 554, object())
        received, on_receive = _collect()
        with pytest.raises(_StopPoll):
            await second._stream_events_rpc2(on_receive, ["VideoMotion"], 0)

        events = [event for data, _ in received for event in parse_event(data.decode())]
        assert [(e["Code"], e["action"]) for e in events] == [("VideoMotion", "Start")]
        assert client_module._HOST_RPC2_EVENT_STATE == {
            "cam:80": {("VideoMotion", 0)},
            "cam:8080": {("VideoMotion", 0)},
        }
        assert _rpc2_poll_block(SimpleNamespace(_address="cam", _port=8080))[
            "active"
        ] == ["VideoMotion-0"]
        assert _rpc2_poll_block(SimpleNamespace(_address="cam", _port=8081)) == {
            "used": False
        }
