"""Adding a channel to a single camera must not lose the channel it already had.

#1236: a Lorex N910A6 recorder was added with one channel, so it was set up as a
flat entry, a single camera with its channel in the entry's own `data` (#940).
"Add a channel" (#969) then added subentries for channels 1 to 3, and
`channel_configs()` reads channels from the subentries whenever there are any.
Channel 0 stopped being set up at all: no coordinator, and every one of its
entities unavailable, on every reload from then on.

The repair gives the first channel a subentry of its own, which is the shape
setup builds for a recorder added with several channels (`_channel_subentries`),
and re-files its device and entities there. These tests run against Home
Assistant's real registries, because what can go wrong is in them: moving a
device to another subentry deletes every entity of it still on the old one.
"""

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigSubentry
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components import dahua as dahua_module
from custom_components.dahua import channel_configs
from custom_components.dahua.const import DOMAIN
from custom_components.dahua.migrate import async_adopt_stranded_primary

ADDRESS = "10.9.9.8"
SERIAL = "8L0ABCD1234"
DATA = {
    "username": "u",
    "password": "p",
    "address": ADDRESS,
    "port": 80,
    "rtsp_port": 554,
    "name": "Front",
    "channel": 0,
    "events": ["VideoMotion"],
}


@pytest.fixture(autouse=True)
def _clean_connectors():
    dahua_module._HOST_CONNECTORS.clear()
    yield
    dahua_module._HOST_CONNECTORS.clear()


def _channels(entry):
    return [config["channel"] for _, config in channel_configs(entry)]


def _subentry(channel, name):
    return ConfigSubentry(
        data={**DATA, "channel": channel, "name": name},
        subentry_type="channel",
        title=name,
        unique_id="%s_%d" % (ADDRESS, channel),
    )


def _device(hass, entry, identifier, subentry_id=None):
    return dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        config_subentry_id=subentry_id,
        identifiers={(DOMAIN, identifier)},
    )


def _entity(hass, entry, device, unique_id, subentry_id=None):
    return er.async_get(hass).async_get_or_create(
        "binary_sensor",
        DOMAIN,
        unique_id,
        config_entry=entry,
        config_subentry_id=subentry_id,
        device_id=device.id,
    )


def _single_camera(hass):
    """A flat entry, with the device and entities its one channel registered."""
    entry = MockConfigEntry(domain=DOMAIN, data=dict(DATA), title="Front")
    entry.add_to_hass(hass)
    device = _device(hass, entry, SERIAL)
    rows = [
        _entity(hass, entry, device, "%s_%s" % (SERIAL, name))
        for name in ("motion", "smart_motion_human", "smart_motion_vehicle")
    ]
    return entry, device, rows


def _add_channel(hass, entry, channel):
    subentry = _subentry(channel, "Channel %d" % (channel + 1))
    hass.config_entries.async_add_subentry(entry, subentry)
    device = _device(
        hass, entry, "%s_%d" % (SERIAL, channel), subentry_id=subentry.subentry_id
    )
    _entity(
        hass,
        entry,
        device,
        "%s_%d_motion" % (SERIAL, channel),
        subentry_id=subentry.subentry_id,
    )
    return subentry


# --- the bug ----------------------------------------------------------------


async def test_the_first_channel_survives_a_channel_being_added(hass):
    """#1236 as reported: channels 1 to 3 added, channel 0 gone."""
    entry, _, _ = _single_camera(hass)
    for channel in (1, 2, 3):
        _add_channel(hass, entry, channel)

    async_adopt_stranded_primary(hass, entry)

    assert _channels(entry) == [0, 1, 2, 3]


async def test_its_entities_keep_their_ids_and_follow_it(hass):
    """Nothing is renamed or recreated: the same rows, filed on the new
    subentry, so history and automations carry on."""
    entry, device, rows = _single_camera(hass)
    _add_channel(hass, entry, 1)
    entities = er.async_get(hass)
    before = len(er.async_entries_for_config_entry(entities, entry.entry_id))

    new_id = async_adopt_stranded_primary(hass, entry)

    assert new_id in entry.subentries
    assert entry.subentries[new_id].data["channel"] == 0
    assert dr.async_get(hass).async_get(device.id).config_subentry_id == new_id
    for row in rows:
        moved = entities.async_get(row.entity_id)
        assert moved is not None, "%s was deleted" % row.entity_id
        assert moved.config_subentry_id == new_id
    assert len(er.async_entries_for_config_entry(entities, entry.entry_id)) == before


async def test_the_recorders_own_device_stays_where_setup_files_it(hass):
    """Setup files the box's device on no subentry (`sensor._filed_under`), so
    the repair must not take it for a stranded channel."""
    entry, _, _ = _single_camera(hass)
    host = _device(hass, entry, "%s_host" % SERIAL)
    disk = _entity(hass, entry, host, "%s_host_disk" % SERIAL)
    _add_channel(hass, entry, 1)

    async_adopt_stranded_primary(hass, entry)

    assert dr.async_get(hass).async_get(host.id).config_subentry_id is None
    assert er.async_get(hass).async_get(disk.entity_id).config_subentry_id is None


async def test_it_is_done_once(hass):
    entry, _, _ = _single_camera(hass)
    _add_channel(hass, entry, 1)

    first = async_adopt_stranded_primary(hass, entry)
    second = async_adopt_stranded_primary(hass, entry)

    assert first is not None
    assert second is None
    assert len(entry.subentries) == 2


async def test_the_first_channels_own_settings_go_with_it(hass):
    """A single camera keeps per-channel choices in the entry's options. Once
    the channel has a subentry, that is where `channel_option` reads them."""
    entry, _, _ = _single_camera(hass)
    hass.config_entries.async_update_entry(
        entry, options={"events": ["SmartMotionHuman"]}
    )
    _add_channel(hass, entry, 1)

    new_id = async_adopt_stranded_primary(hass, entry)

    assert entry.subentries[new_id].data["events"] == ["SmartMotionHuman"]


# --- what it must leave alone -----------------------------------------------


async def test_a_single_camera_is_left_alone(hass):
    """No subentries is the single-camera shape (#940), not a broken one."""
    entry, device, _ = _single_camera(hass)

    assert async_adopt_stranded_primary(hass, entry) is None
    assert entry.subentries == {}
    assert dr.async_get(hass).async_get(device.id).config_subentry_id is None


async def test_a_recorder_set_up_with_its_channels_is_left_alone(hass):
    entry = MockConfigEntry(domain=DOMAIN, data=dict(DATA), title="NVR")
    entry.add_to_hass(hass)
    first = _subentry(0, "Front")
    hass.config_entries.async_add_subentry(entry, first)
    _device(hass, entry, SERIAL, subentry_id=first.subentry_id)
    _add_channel(hass, entry, 1)
    _device(hass, entry, "%s_host" % SERIAL)

    assert async_adopt_stranded_primary(hass, entry) is None
    assert len(entry.subentries) == 2


async def test_a_channel_the_user_removed_is_not_brought_back(hass):
    """The entry alone looks the same as #1236's: subentries, and none for the
    channel in its data. Removing the subentry removed its device too, so
    there is nothing on no subentry to say the channel is still wanted."""
    entry = MockConfigEntry(domain=DOMAIN, data=dict(DATA), title="NVR")
    entry.add_to_hass(hass)
    first = _subentry(0, "Front")
    hass.config_entries.async_add_subentry(entry, first)
    _device(hass, entry, SERIAL, subentry_id=first.subentry_id)
    _add_channel(hass, entry, 1)
    _device(hass, entry, "%s_host" % SERIAL)

    hass.config_entries.async_remove_subentry(entry, first.subentry_id)

    assert async_adopt_stranded_primary(hass, entry) is None
    assert _channels(entry) == [1]


# --- through Home Assistant's own flow and setup ----------------------------


def _working():
    return patch.object(
        dahua_module.DahuaDataUpdateCoordinator,
        "_async_update_data",
        return_value={"serialNumber": SERIAL},
    )


def _no_platforms(hass):
    return (
        patch.object(
            dahua_module.DahuaDataUpdateCoordinator, "async_start_event_listener"
        ),
        patch.object(hass.config_entries, "async_forward_entry_setups", AsyncMock()),
        patch.object(
            hass.config_entries, "async_unload_platforms", AsyncMock(return_value=True)
        ),
    )


async def test_add_a_channel_end_to_end(hass, enable_custom_integrations):
    """Press "Add a channel" on a loaded single camera and let the reload it
    triggers run: both channels must come up, the first on a subentry of its
    own, and its entity must still be there."""
    entry, device, rows = _single_camera(hass)
    starts, forward, unload = _no_platforms(hass)

    with _working(), starts, forward, unload:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert sorted(entry.runtime_data) == [0]

        flow = await hass.config_entries.subentries.async_init(
            (entry.entry_id, "channel"), context={"source": "user"}
        )
        result = await hass.config_entries.subentries.async_configure(
            flow["flow_id"],
            {"channel": 1, "name": "Garage", "area": "", "events": ["VideoMotion"]},
        )
        assert result["type"] == "create_entry"
        await hass.async_block_till_done()

        assert sorted(entry.runtime_data) == [
            0,
            1,
        ], "channel 0 is no longer set up after a channel was added"
        first = [
            subentry_id
            for subentry_id, subentry in entry.subentries.items()
            if subentry.data["channel"] == 0
        ]
        assert len(first) == 1
        assert dr.async_get(hass).async_get(device.id).config_subentry_id == first[0]
        assert er.async_get(hass).async_get(rows[0].entity_id) is not None
