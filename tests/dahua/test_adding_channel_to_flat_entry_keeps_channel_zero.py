"""Adding channels to a flat single-camera entry preserves channel 0 (#1236).

When setup finds no other channels (e.g. a read-only recorder account whose
``RemoteDevice`` call returns 403), the entry starts flat (no subentries) with
channel 0's device and entities filed at ``config_subentry_id=None``. Adding
channels later via **Add a channel** must:

1. Promote channel 0 to its own subentry so ``channel_configs`` keeps it alive.
2. Move channel 0's existing entities and device onto channel 0's subentry
   before platforms reload — entities first, then the device — leaving any
   separate recorder host device (``<serial>_host``) untouched at ``None``.
"""

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import ConfigEntryState, ConfigSubentry, SOURCE_USER
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

import custom_components.dahua as dahua_module
from custom_components.dahua import channel_configs, migrate
from custom_components.dahua.config_flow import DahuaChannelSubentryFlow
from custom_components.dahua.const import (
    CONF_ADDRESS,
    CONF_AREA,
    CONF_CHANNEL,
    CONF_EVENTS,
    CONF_NAME,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_RTSP_PORT,
    CONF_USERNAME,
    DOMAIN,
)
from custom_components.dahua.migrate import CHANNEL_SUBENTRY

SERIAL = "SN123"
HOST_ADDRESS = "192.0.2.10"

BASE_DATA = {
    CONF_ADDRESS: HOST_ADDRESS,
    CONF_USERNAME: "viewer",
    CONF_PASSWORD: "secret",
    CONF_PORT: "80",
    CONF_RTSP_PORT: "554",
    CONF_CHANNEL: 0,
    CONF_NAME: "Channel 1",
}


def _flat_entry_with_registries(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=dict(BASE_DATA),
        options={CONF_EVENTS: ["SmartMotionHuman"]},
        unique_id=SERIAL,
        title="Lorex NVR",
    )
    entry.add_to_hass(hass)

    devices = dr.async_get(hass)
    entities = er.async_get(hass)

    ch0_device = devices.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, SERIAL)},
        name="Channel 1",
    )
    cam_row = entities.async_get_or_create(
        "camera",
        DOMAIN,
        f"{SERIAL}_cam",
        config_entry=entry,
        device_id=ch0_device.id,
        suggested_object_id="channel_1_cam",
    )
    sm_row = entities.async_get_or_create(
        "binary_sensor",
        DOMAIN,
        f"{SERIAL}_smart_motion_human",
        config_entry=entry,
        device_id=ch0_device.id,
        suggested_object_id="channel_1_smart_motion_human",
    )

    host_device = devices.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, f"{SERIAL}_host")},
        name="Lorex NVR",
    )
    disk_row = entities.async_get_or_create(
        "binary_sensor",
        DOMAIN,
        f"{SERIAL}_disk_0",
        config_entry=entry,
        device_id=host_device.id,
        suggested_object_id="lorex_nvr_disk_0",
    )
    return entry, ch0_device, [cam_row, sm_row], host_device, disk_row


def _coordinator_stub(channel: int, subentry_id: str | None):
    serial = SERIAL if channel == 0 else f"{SERIAL}_{channel}"
    return SimpleNamespace(
        subentry_id=subentry_id,
        get_channel=lambda: channel,
        get_serial_number=lambda: serial,
        get_device_serial_number=lambda: SERIAL,
        host_device_identifier=lambda: f"{SERIAL}_host",
    )


async def test_adding_channel_to_flat_entry_keeps_channel_zero_and_avoids_subentry_warning(
    hass, caplog
):
    entry, ch0_device, ch0_rows, host_device, disk_row = _flat_entry_with_registries(
        hass
    )
    before_entity_ids = [row.entity_id for row in ch0_rows]

    flow = DahuaChannelSubentryFlow()
    flow.hass = hass
    flow._get_entry = lambda: entry
    flow.async_create_entry = lambda **kwargs: {"type": "create_entry", **kwargs}

    result = await flow.async_step_user(
        {
            CONF_CHANNEL: 1,
            CONF_NAME: "Channel 2",
            CONF_AREA: "",
            CONF_EVENTS: ["SmartMotionVehicle"],
        }
    )
    assert result["type"] == "create_entry"

    # Simulate ConfigSubentryFlowManager.async_finish_flow persisting channel 1.
    hass.config_entries.async_add_subentry(
        entry,
        ConfigSubentry(
            data=result["data"],
            subentry_type=CHANNEL_SUBENTRY,
            title=result["title"],
            unique_id=result["unique_id"],
        ),
    )

    configs = channel_configs(entry)
    assert [cfg[CONF_CHANNEL] for _, cfg in configs] == [0, 1]
    sub_0, sub_1 = configs[0][0], configs[1][0]
    assert sub_0 is not None and sub_1 is not None and sub_0 != sub_1

    coordinators = [_coordinator_stub(0, sub_0), _coordinator_stub(1, sub_1)]
    moved = migrate.async_adopt_flat_channel_rows(hass, entry, coordinators)
    assert moved == 2

    devices = dr.async_get(hass)
    entities = er.async_get(hass)

    with caplog.at_level(logging.WARNING):
        devices.async_get_or_create(
            config_entry_id=entry.entry_id,
            config_subentry_id=sub_0,
            identifiers={(DOMAIN, SERIAL)},
            name="Channel 1",
        )
        devices.async_get_or_create(
            config_entry_id=entry.entry_id,
            config_subentry_id=sub_1,
            identifiers={(DOMAIN, f"{SERIAL}_1")},
            name="Channel 2",
        )
        devices.async_get_or_create(
            config_entry_id=entry.entry_id,
            config_subentry_id=None,
            identifiers={(DOMAIN, f"{SERIAL}_host")},
            name="Lorex NVR",
        )

    assert (
        "assigns an existing device to a different config subentry" not in caplog.text
    )

    for eid in before_entity_ids:
        row = entities.async_get(eid)
        assert row is not None
        assert row.entity_id == eid
        assert row.config_subentry_id == sub_0

    refreshed_ch0 = devices.async_get(ch0_device.id)
    assert refreshed_ch0 is not None
    assert refreshed_ch0.config_subentry_id == sub_0

    refreshed_host = devices.async_get(host_device.id)
    assert refreshed_host is not None
    assert refreshed_host.config_subentry_id is None

    refreshed_disk = entities.async_get(disk_row.entity_id)
    assert refreshed_disk is not None
    assert refreshed_disk.config_subentry_id is None


async def test_recovery_when_channel_zero_rows_were_stranded_at_none(hass, caplog):
    """An entry that already gained sub_1 under the old bug recovers cleanly once
    channel 0 has a subentry and setup runs async_adopt_flat_channel_rows."""
    entry, ch0_device, ch0_rows, _, _ = _flat_entry_with_registries(hass)
    before_entity_ids = [row.entity_id for row in ch0_rows]

    sub_1 = ConfigSubentry(
        data={**BASE_DATA, CONF_CHANNEL: 1, CONF_NAME: "Channel 2"},
        subentry_type=CHANNEL_SUBENTRY,
        title="Channel 2",
        unique_id=f"{HOST_ADDRESS}_1",
    )
    hass.config_entries.async_add_subentry(entry, sub_1)
    sub_0 = ConfigSubentry(
        data={**BASE_DATA, CONF_CHANNEL: 0, CONF_NAME: "Channel 1"},
        subentry_type=CHANNEL_SUBENTRY,
        title="Channel 1",
        unique_id=f"{HOST_ADDRESS}_0",
    )
    hass.config_entries.async_add_subentry(entry, sub_0)

    coordinators = [
        _coordinator_stub(0, sub_0.subentry_id),
        _coordinator_stub(1, sub_1.subentry_id),
    ]
    moved = migrate.async_adopt_flat_channel_rows(hass, entry, coordinators)
    assert moved == 2

    devices = dr.async_get(hass)
    entities = er.async_get(hass)
    with caplog.at_level(logging.WARNING):
        devices.async_get_or_create(
            config_entry_id=entry.entry_id,
            config_subentry_id=sub_0.subentry_id,
            identifiers={(DOMAIN, SERIAL)},
            name="Channel 1",
        )

    assert (
        "assigns an existing device to a different config subentry" not in caplog.text
    )
    assert devices.async_get(ch0_device.id).config_subentry_id == sub_0.subentry_id
    for eid in before_entity_ids:
        assert entities.async_get(eid).config_subentry_id == sub_0.subentry_id


async def test_flat_channel_adoption_is_idempotent(hass):
    entry, ch0_device, ch0_rows, _, _ = _flat_entry_with_registries(hass)
    sub_0 = ConfigSubentry(
        data={**BASE_DATA, CONF_CHANNEL: 0, CONF_NAME: "Channel 1"},
        subentry_type=CHANNEL_SUBENTRY,
        title="Channel 1",
        unique_id=f"{HOST_ADDRESS}_0",
    )
    hass.config_entries.async_add_subentry(entry, sub_0)
    coordinators = [_coordinator_stub(0, sub_0.subentry_id)]

    assert migrate.async_adopt_flat_channel_rows(hass, entry, coordinators) == 2
    assert migrate.async_adopt_flat_channel_rows(hass, entry, coordinators) == 0

    entities = er.async_get(hass)
    devices = dr.async_get(hass)
    assert devices.async_get(ch0_device.id).config_subentry_id == sub_0.subentry_id
    for row in ch0_rows:
        assert entities.async_get(row.entity_id).config_subentry_id == sub_0.subentry_id


async def test_entities_move_before_device(hass, monkeypatch):
    """Home Assistant deletes entities when a device changes owner/subentry ahead of
    them, so entity platform updates must happen before the device update."""
    entry, _, _, _, _ = _flat_entry_with_registries(hass)
    sub_0 = ConfigSubentry(
        data={**BASE_DATA, CONF_CHANNEL: 0, CONF_NAME: "Channel 1"},
        subentry_type=CHANNEL_SUBENTRY,
        title="Channel 1",
        unique_id=f"{HOST_ADDRESS}_0",
    )
    hass.config_entries.async_add_subentry(entry, sub_0)

    entities = er.async_get(hass)
    devices = dr.async_get(hass)
    order = []
    real_update_entity = entities.async_update_entity_platform
    real_update_device = devices.async_update_device

    def spy_entity(entity_id, platform, **kwargs):
        order.append(("entity", entity_id))
        return real_update_entity(entity_id, platform, **kwargs)

    def spy_device(device_id, **kwargs):
        order.append(("device", device_id))
        return real_update_device(device_id, **kwargs)

    monkeypatch.setattr(entities, "async_update_entity_platform", spy_entity)
    monkeypatch.setattr(devices, "async_update_device", spy_device)

    migrate.async_adopt_flat_channel_rows(
        hass, entry, [_coordinator_stub(0, sub_0.subentry_id)]
    )

    assert [kind for kind, _ in order] == ["entity", "entity", "device"]


async def test_setup_entry_adopts_flat_rows_before_forwarding_platforms(hass):
    entry, ch0_device, ch0_rows, _, _ = _flat_entry_with_registries(hass)
    sub_0 = ConfigSubentry(
        data={**BASE_DATA, CONF_CHANNEL: 0, CONF_NAME: "Channel 1"},
        subentry_type=CHANNEL_SUBENTRY,
        title="Channel 1",
        unique_id=f"{HOST_ADDRESS}_0",
    )
    hass.config_entries.async_add_subentry(entry, sub_0)
    entry._async_set_state(hass, ConfigEntryState.SETUP_IN_PROGRESS, None)

    seen_at_forward = {}

    async def fake_forward(_entry, _platforms):
        entities = er.async_get(hass)
        devices = dr.async_get(hass)
        seen_at_forward["device_subentry"] = devices.async_get(
            ch0_device.id
        ).config_subentry_id
        seen_at_forward["entity_subentries"] = [
            entities.async_get(r.entity_id).config_subentry_id for r in ch0_rows
        ]
        return True

    def fake_coordinator(
        _hass,
        *,
        entry,
        events,
        address,
        port,
        rtsp_port,
        username,
        password,
        name,
        channel,
        use_https,
        channel_config,
        subentry_id,
    ):
        return SimpleNamespace(
            subentry_id=subentry_id,
            platforms=[],
            get_address=lambda: address,
            get_channel=lambda: channel,
            get_serial_number=lambda: SERIAL,
            get_device_serial_number=lambda: SERIAL,
            host_device_identifier=lambda: f"{SERIAL}_host",
            client=SimpleNamespace(),
            async_config_entry_first_refresh=AsyncMock(return_value=None),
            async_stop=AsyncMock(return_value=None),
        )

    with (
        patch.object(
            dahua_module, "DahuaDataUpdateCoordinator", side_effect=fake_coordinator
        ),
        patch.object(
            hass.config_entries, "async_forward_entry_setups", side_effect=fake_forward
        ),
        patch.object(
            hass.config_entries, "async_unload_platforms", AsyncMock(return_value=True)
        ),
    ):
        assert await dahua_module.async_setup_entry(hass, entry) is True

    assert seen_at_forward["device_subentry"] == sub_0.subentry_id
    assert seen_at_forward["entity_subentries"] == [
        sub_0.subentry_id,
        sub_0.subentry_id,
    ]
    await dahua_module.async_unload_entry(hass, entry)


async def test_upgrade_recovers_stranded_primary_subentry_during_setup(hass):
    entry, ch0_device, ch0_rows, host_device, disk_row = _flat_entry_with_registries(
        hass
    )
    # Simulate an entry left in pre-fix #1236 state: channel 1 subentry added,
    # channel 0 left only in entry.data with device and entities at subentry=None.
    sub_1 = ConfigSubentry(
        data={**BASE_DATA, CONF_CHANNEL: 1, CONF_NAME: "Channel 2"},
        subentry_type=CHANNEL_SUBENTRY,
        title="Channel 2",
        unique_id=f"{HOST_ADDRESS}_1",
    )
    hass.config_entries.async_add_subentry(entry, sub_1)
    entry._async_set_state(hass, ConfigEntryState.SETUP_IN_PROGRESS, None)

    built_channels: list[int] = []
    seen_at_forward: dict[str, object] = {}

    async def fake_forward(_entry, _platforms):
        entities = er.async_get(hass)
        devices = dr.async_get(hass)
        seen_at_forward["device_subentry"] = devices.async_get(
            ch0_device.id
        ).config_subentry_id
        seen_at_forward["host_subentry"] = devices.async_get(
            host_device.id
        ).config_subentry_id
        seen_at_forward["entity_subentries"] = [
            entities.async_get(r.entity_id).config_subentry_id for r in ch0_rows
        ]
        seen_at_forward["disk_subentry"] = entities.async_get(
            disk_row.entity_id
        ).config_subentry_id
        return True

    def fake_coordinator(
        _hass,
        *,
        entry,
        events,
        address,
        port,
        rtsp_port,
        username,
        password,
        name,
        channel,
        use_https,
        channel_config,
        subentry_id,
    ):
        built_channels.append(channel)
        serial = SERIAL if channel == 0 else f"{SERIAL}_{channel}"
        return SimpleNamespace(
            subentry_id=subentry_id,
            platforms=[],
            get_address=lambda: address,
            get_channel=lambda: channel,
            get_serial_number=lambda: serial,
            get_device_serial_number=lambda: SERIAL,
            host_device_identifier=lambda: f"{SERIAL}_host",
            client=SimpleNamespace(),
            async_config_entry_first_refresh=AsyncMock(return_value=None),
            async_stop=AsyncMock(return_value=None),
        )

    with (
        patch.object(
            dahua_module, "DahuaDataUpdateCoordinator", side_effect=fake_coordinator
        ),
        patch.object(
            hass.config_entries, "async_forward_entry_setups", side_effect=fake_forward
        ),
        patch.object(
            hass.config_entries, "async_unload_platforms", AsyncMock(return_value=True)
        ),
    ):
        assert await dahua_module.async_setup_entry(hass, entry) is True

    recovered_subs = {
        int(sub.data[CONF_CHANNEL]): sub for sub in entry.subentries.values()
    }
    assert set(recovered_subs) == {0, 1}
    recovered_0 = recovered_subs[0]
    assert recovered_0.title == "Channel 1"
    assert recovered_0.data[CONF_EVENTS] == ["SmartMotionHuman"]
    assert recovered_0.unique_id == f"{HOST_ADDRESS}_0"
    assert sorted(built_channels) == [0, 1]
    assert seen_at_forward["device_subentry"] == recovered_0.subentry_id
    assert seen_at_forward["entity_subentries"] == [
        recovered_0.subentry_id,
        recovered_0.subentry_id,
    ]
    assert seen_at_forward["host_subentry"] is None
    assert seen_at_forward["disk_subentry"] is None
    await dahua_module.async_unload_entry(hass, entry)


async def test_deliberately_removed_primary_channel_is_not_restored(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=dict(BASE_DATA),
        unique_id=SERIAL,
        title="Lorex NVR",
    )
    entry.add_to_hass(hass)

    sub_1 = ConfigSubentry(
        data={**BASE_DATA, CONF_CHANNEL: 1, CONF_NAME: "Channel 2"},
        subentry_type=CHANNEL_SUBENTRY,
        title="Channel 2",
        unique_id=f"{HOST_ADDRESS}_1",
    )
    hass.config_entries.async_add_subentry(entry, sub_1)

    devices = dr.async_get(hass)
    entities = er.async_get(hass)

    # Separate recorder host device & disk entity remain at config_subentry_id=None,
    # while channel 1 owns its own subentry rows and channel 0 has been deleted.
    host_device = devices.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, f"{SERIAL}_host")},
        name="Lorex NVR",
    )
    entities.async_get_or_create(
        "binary_sensor",
        DOMAIN,
        f"{SERIAL}_disk_0",
        config_entry=entry,
        device_id=host_device.id,
        suggested_object_id="lorex_nvr_disk_0",
    )
    ch1_device = devices.async_get_or_create(
        config_entry_id=entry.entry_id,
        config_subentry_id=sub_1.subentry_id,
        identifiers={(DOMAIN, f"{SERIAL}_1")},
        name="Channel 2",
    )
    entities.async_get_or_create(
        "camera",
        DOMAIN,
        f"{SERIAL}_1_cam",
        config_entry=entry,
        config_subentry_id=sub_1.subentry_id,
        device_id=ch1_device.id,
        suggested_object_id="channel_2_cam",
    )

    assert migrate.async_recover_stranded_primary_subentry(hass, entry) is False
    assert [int(sub.data[CONF_CHANNEL]) for sub in entry.subentries.values()] == [1]


async def test_first_add_channel_through_subentry_flow_manager_reloads_once(
    hass, enable_custom_integrations
):
    entry, _, _, _, _ = _flat_entry_with_registries(hass)
    entry._async_set_state(hass, ConfigEntryState.LOADED, None)
    entry.async_on_unload(entry.add_update_listener(dahua_module.async_reload_entry))

    with patch.object(
        hass.config_entries, "async_reload", AsyncMock(return_value=True)
    ) as reload_mock:
        init_result = await hass.config_entries.subentries.async_init(
            (entry.entry_id, CHANNEL_SUBENTRY),
            context={"source": SOURCE_USER},
        )
        configure_result = await hass.config_entries.subentries.async_configure(
            init_result["flow_id"],
            user_input={
                CONF_CHANNEL: 1,
                CONF_NAME: "Channel 2",
                CONF_EVENTS: ["CrossLineDetection"],
            },
        )
        await hass.async_block_till_done()

    assert configure_result["type"] == "create_entry"
    assert {int(sub.data[CONF_CHANNEL]) for sub in entry.subentries.values()} == {0, 1}
    assert reload_mock.await_count == 1


async def test_reload_coalescing_does_not_drop_update_during_inflight_reload(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=dict(BASE_DATA),
        unique_id=SERIAL,
        title="Lorex NVR",
    )
    entry.add_to_hass(hass)
    entry._async_set_state(hass, ConfigEntryState.LOADED, None)
    entry.async_on_unload(entry.add_update_listener(dahua_module.async_reload_entry))

    first_reload_started = asyncio.Event()
    release_first_reload = asyncio.Event()
    reload_calls = 0

    async def slow_first_reload(_entry_id: str) -> bool:
        nonlocal reload_calls
        reload_calls += 1
        if reload_calls == 1:
            first_reload_started.set()
            await release_first_reload.wait()
        return True

    with patch.object(
        hass.config_entries, "async_reload", side_effect=slow_first_reload
    ):
        hass.async_create_task(dahua_module.async_reload_entry(hass, entry))
        await first_reload_started.wait()

        # While the first reload is in progress, a subsequent update must still
        # schedule a follow-up reload rather than being swallowed as duplicate.
        hass.config_entries.async_update_entry(
            entry, options={**entry.options, CONF_EVENTS: ["SmartMotionVehicle"]}
        )
        await asyncio.sleep(0)
        release_first_reload.set()
        await hass.async_block_till_done()

    assert reload_calls == 2
