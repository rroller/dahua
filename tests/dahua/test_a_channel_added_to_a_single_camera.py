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
from custom_components.dahua.const import CONF_ADOPTION_PENDING, DOMAIN
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


async def test_entry_wide_labels_stay_on_the_entry(hass):
    """`_channel_subentries` keeps labels out of every channel's data, so the
    repaired entry has the shape setup would have built."""
    entry, _, _ = _single_camera(hass)
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, "labels": ["outside"]}
    )
    _add_channel(hass, entry, 1)

    new_id = async_adopt_stranded_primary(hass, entry)

    assert "labels" not in entry.subentries[new_id].data


async def test_entities_that_will_not_move_leave_everything_as_it_was(hass):
    """Moving the device with an entity left behind would delete that entity,
    and so would leaving the new subentry for setup to file the device on. So
    a move that does not take is undone, rows first."""
    entry, device, rows = _single_camera(hass)
    _add_channel(hass, entry, 1)
    entities = er.async_get(hass)
    real_update = entities.async_update_entity
    stuck = rows[0].entity_id

    def update(entity_id, **kwargs):
        if entity_id == stuck and kwargs.get("config_subentry_id") is not None:
            return entities.async_get(entity_id)
        return real_update(entity_id, **kwargs)

    with patch.object(entities, "async_update_entity", side_effect=update):
        result = async_adopt_stranded_primary(hass, entry)

    assert result is None
    assert len(entry.subentries) == 1
    assert dr.async_get(hass).async_get(device.id).config_subentry_id is None
    for row in rows:
        kept = entities.async_get(row.entity_id)
        assert kept is not None, "%s was deleted" % row.entity_id
        assert kept.config_subentry_id is None


# --- a move that raises (rroller's review of #1239) ---------------------------


def _as_it_was(hass, entry, device, rows):
    """Nothing added, nothing moved, nothing deleted."""
    entities = er.async_get(hass)
    assert len(entry.subentries) == 1
    assert _channels(entry) == [1]
    assert dr.async_get(hass).async_get(device.id).config_subentry_id is None
    for row in rows:
        kept = entities.async_get(row.entity_id)
        assert kept is not None, "%s was deleted" % row.entity_id
        assert kept.config_subentry_id is None


def _raising_on(entities, entity_id, when=lambda kwargs: True):
    real_update = entities.async_update_entity

    def update(updated, **kwargs):
        if updated == entity_id and when(kwargs):
            raise ValueError("injected")
        return real_update(updated, **kwargs)

    return patch.object(entities, "async_update_entity", side_effect=update)


async def test_an_entity_update_that_raises_after_one_row_moved_is_undone(hass):
    """The exception used to leave the new subentry in place with one row on
    it. Setup carried on from there."""
    entry, device, rows = _single_camera(hass)
    _add_channel(hass, entry, 1)
    entities = er.async_get(hass)
    moved = []
    real_update = entities.async_update_entity

    def update(entity_id, **kwargs):
        if kwargs.get("config_subentry_id") is not None:
            if moved:
                raise ValueError("injected")
            moved.append(entity_id)
        return real_update(entity_id, **kwargs)

    with patch.object(entities, "async_update_entity", side_effect=update):
        result = async_adopt_stranded_primary(hass, entry)

    assert len(moved) == 1, "one row had already moved when the next raised"
    assert result is None
    _as_it_was(hass, entry, device, rows)


async def test_setup_after_a_move_that_raised_deletes_nothing(
    hass, enable_custom_integrations
):
    """The whole of what rroller reproduced: with the subentry left behind,
    setup brought channel 0 up on it, the device was filed there, and Home
    Assistant deleted the rows still on the old subentry."""
    entry, device, rows = _single_camera(hass)
    _add_channel(hass, entry, 1)
    entities = er.async_get(hass)
    starts, forward, unload = _no_platforms(hass)

    with _working(), starts, forward, unload, _raising_on(
        entities,
        rows[1].entity_id,
        when=lambda kwargs: kwargs.get("config_subentry_id") is not None,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        assert sorted(entry.runtime_data) == [1]
    _as_it_was(hass, entry, device, rows)


async def test_a_device_update_that_raises_is_undone(hass):
    """Every row is on the new subentry by then, so they go back first."""
    entry, device, rows = _single_camera(hass)
    _add_channel(hass, entry, 1)
    devices = dr.async_get(hass)
    real_update = devices.async_update_device

    def update(device_id, **kwargs):
        if kwargs.get("new_config_subentry_id") is not None:
            raise ValueError("injected")
        return real_update(device_id, **kwargs)

    with patch.object(devices, "async_update_device", side_effect=update):
        result = async_adopt_stranded_primary(hass, entry)

    assert result is None
    _as_it_was(hass, entry, device, rows)


async def test_a_device_that_will_not_move_is_undone(hass):
    entry, device, rows = _single_camera(hass)
    _add_channel(hass, entry, 1)
    devices = dr.async_get(hass)
    real_update = devices.async_update_device

    def update(device_id, **kwargs):
        if kwargs.get("new_config_subentry_id") is not None:
            return devices.async_get(device_id)
        return real_update(device_id, **kwargs)

    with patch.object(devices, "async_update_device", side_effect=update):
        result = async_adopt_stranded_primary(hass, entry)

    assert result is None
    _as_it_was(hass, entry, device, rows)


async def test_a_failure_at_the_very_last_step_is_undone(hass):
    """Rows and device are all on the subentry; only the mark is left."""
    entry, device, rows = _single_camera(hass)
    _add_channel(hass, entry, 1)

    with patch.object(
        hass.config_entries, "async_update_subentry", side_effect=ValueError("no")
    ):
        result = async_adopt_stranded_primary(hass, entry)

    assert result is None
    _as_it_was(hass, entry, device, rows)


# --- setup cannot use the subentry before the move has finished ---------------


async def test_a_finished_move_leaves_no_mark(hass):
    entry, _, _ = _single_camera(hass)
    _add_channel(hass, entry, 1)

    new_id = async_adopt_stranded_primary(hass, entry)

    assert CONF_ADOPTION_PENDING not in entry.subentries[new_id].data
    assert all(
        CONF_ADOPTION_PENDING not in config for _, config in channel_configs(entry)
    )


async def test_a_pending_subentry_is_not_set_up(hass):
    entry, _, _ = _single_camera(hass)
    _add_channel(hass, entry, 1)
    pending = ConfigSubentry(
        data={**DATA, "channel": 0, CONF_ADOPTION_PENDING: True},
        subentry_type="channel",
        title="Front",
        unique_id="%s_0" % ADDRESS,
    )
    hass.config_entries.async_add_subentry(entry, pending)

    assert _channels(entry) == [1]


async def test_the_subentry_is_pending_while_rows_are_moving(hass):
    """So a failure at any point in between finds setup already shut out."""
    entry, _, rows = _single_camera(hass)
    _add_channel(hass, entry, 1)
    entities = er.async_get(hass)
    devices = dr.async_get(hass)
    seen = []
    real_entity = entities.async_update_entity
    real_device = devices.async_update_device

    def entity_update(entity_id, **kwargs):
        seen.append(_channels(entry))
        return real_entity(entity_id, **kwargs)

    def device_update(device_id, **kwargs):
        seen.append(_channels(entry))
        return real_device(device_id, **kwargs)

    with patch.object(
        entities, "async_update_entity", side_effect=entity_update
    ), patch.object(devices, "async_update_device", side_effect=device_update):
        async_adopt_stranded_primary(hass, entry)

    assert seen == [[1]] * (len(rows) + 1)
    assert _channels(entry) == [0, 1]


async def test_a_move_that_cannot_be_undone_is_kept_out_of_setup_and_finished_later(
    hass,
):
    """Both directions fail: the second row will not move, and the first will
    not go back. Removing the subentry would delete the first row, and letting
    setup use it would delete the other two. So it is kept, pending, and the
    next setup finishes it."""
    entry, device, rows = _single_camera(hass)
    _add_channel(hass, entry, 1)
    entities = er.async_get(hass)
    real_update = entities.async_update_entity
    first, second = rows[0].entity_id, rows[1].entity_id

    def update(entity_id, **kwargs):
        target = kwargs.get("config_subentry_id", "absent")
        if entity_id == second and target not in (None, "absent"):
            raise ValueError("will not move")
        if entity_id == first and target is None:
            raise ValueError("will not go back")
        return real_update(entity_id, **kwargs)

    with patch.object(entities, "async_update_entity", side_effect=update):
        assert async_adopt_stranded_primary(hass, entry) is None

    assert len(entry.subentries) == 2
    pending = [
        sub for sub in entry.subentries.values() if sub.data.get(CONF_ADOPTION_PENDING)
    ]
    assert len(pending) == 1
    assert _channels(entry) == [1], "setup must not bring channel 0 up on it"
    assert dr.async_get(hass).async_get(device.id).config_subentry_id is None
    for row in rows:
        assert entities.async_get(row.entity_id) is not None, "a row was deleted"

    result = async_adopt_stranded_primary(hass, entry)

    assert result == pending[0].subentry_id
    assert len(entry.subentries) == 2, "finished, not started a second time"
    assert _channels(entry) == [0, 1]
    assert dr.async_get(hass).async_get(device.id).config_subentry_id == result
    for row in rows:
        assert entities.async_get(row.entity_id).config_subentry_id == result


async def test_the_undo_does_not_move_a_device_away_from_a_row_it_could_not_move(
    hass,
):
    """The last step fails with everything on the new subentry, and in the
    undo one row will not go back. Moving its device back anyway would make
    Home Assistant delete that row. So the device stays, the subentry is kept
    pending, and the next setup finishes it."""
    entry, device, rows = _single_camera(hass)
    _add_channel(hass, entry, 1)
    entities = er.async_get(hass)
    real_update = entities.async_update_entity
    stuck = rows[0].entity_id

    def update(entity_id, **kwargs):
        if entity_id == stuck and kwargs.get("config_subentry_id", "absent") is None:
            raise ValueError("will not go back")
        return real_update(entity_id, **kwargs)

    with patch.object(
        hass.config_entries, "async_update_subentry", side_effect=ValueError("no")
    ), patch.object(entities, "async_update_entity", side_effect=update):
        assert async_adopt_stranded_primary(hass, entry) is None

    pending = [
        sub for sub in entry.subentries.values() if sub.data.get(CONF_ADOPTION_PENDING)
    ]
    assert len(pending) == 1
    assert _channels(entry) == [1]
    for row in rows:
        assert entities.async_get(row.entity_id) is not None, (
            "%s was deleted" % row.entity_id
        )
    assert (
        dr.async_get(hass).async_get(device.id).config_subentry_id
        == pending[0].subentry_id
    ), "the device left a row behind on the subentry"

    result = async_adopt_stranded_primary(hass, entry)

    assert result == pending[0].subentry_id
    assert _channels(entry) == [0, 1]
    for row in rows:
        assert entities.async_get(row.entity_id).config_subentry_id == result


async def test_a_pending_subentry_whose_rows_all_arrived_is_finished(hass):
    """Stopped between the device moving and the mark coming off."""
    entry, device, rows = _single_camera(hass)
    _add_channel(hass, entry, 1)
    with patch.object(
        hass.config_entries, "async_update_subentry", side_effect=ValueError("no")
    ), patch("custom_components.dahua.migrate._undo_adoption", return_value=False):
        assert async_adopt_stranded_primary(hass, entry) is None
    assert _channels(entry) == [1]
    assert all(
        er.async_get(hass).async_get(row.entity_id).config_subentry_id is not None
        for row in rows
    ), "every row had arrived before it stopped"

    result = async_adopt_stranded_primary(hass, entry)

    assert _channels(entry) == [0, 1]
    assert dr.async_get(hass).async_get(device.id).config_subentry_id == result
    assert len(entry.subentries) == 2


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
        # The coordinator is what the platforms file entities under. Carrying
        # None here would put channel 0's entities back on no subentry.
        assert entry.runtime_data[0].subentry_id == first[0]
