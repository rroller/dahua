"""A ring volume per door station, from the monitor's VTOInfo table (#949).

@kmich proposed a Number backed by `VTOInfo.VtoNN.RingVolume` and it sat open for
three weeks, because a monitor ALSO carries `VideoTalkPhoneGeneral.RingVolume`,
which already ships as `DahuaMonitorVolumeNumber`. Two fields of the same name on
one device, and nothing said whether they were two settings or two views of one.
Shipping the second slider blind risked a control that moves nothing, which is
what `Lighting` versus `Lighting_V2` cost elsewhere here.

**Measured 2026-10-23, by ear, on two DHI-VTH5221D (3.000.0012000.0.R).** Both
monitors' `VTOInfo.Vto00.RingVolume` taken from 70 to 10, the VTO2000A's button
pressed, and the owner reported both rang audibly quieter. Read back during the
test, `VideoTalkPhoneGeneral.RingVolume` was still **70** on both. So they are two
settings and this one drives the speaker. The captured tables were written back
and read identical, twenty slots before and after.

⚠️ **What was NOT measured is whether the monitor-wide one does anything**, and
the converse test (that field to 10, this one left at 70, one more press) was not
run. So these tests say nothing about the other slider, and neither does the code.

⚠️ The read rides on `vth_camera_links`, which already fetches the whole VTOInfo
table for the camera-link select. A second read to carry one integer would be a
second login on a device that logs every one, so the reader's shape changed rather
than gaining a sibling. `test_vth_camera_link.py` asserts that whole shape and is
what fails if this field is dropped.
"""

import json
import pathlib
import re
from types import SimpleNamespace

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.dahua import DahuaDataUpdateCoordinator
from custom_components.dahua.client import vth_camera_links
from custom_components.dahua.const import NUMBER, SELECT
from custom_components.dahua.number import (
    DahuaVtoRingVolumeNumber,
    async_setup_entry,
)

from . import adds_entities

# ⚠️ Imported rather than copied. These model one VTH behind RPC2 and the write
# under test is the same whole-table write the camera link does; a second copy
# here would be a second thing to keep in step, and the stale copy is the one
# nobody looks at (memory: tests-must-derive-not-duplicate).
from .test_vth_camera_link import _camera_table, _client, _copy, _Device, _vto_table

# --- reading the volume off the slot ------------------------------------------


def test_the_measured_slot_reports_its_ring_volume():
    links = vth_camera_links(_vto_table(), _camera_table())

    assert links["vtos"]["Vto00"]["ring_volume"] == 70


def test_a_slot_with_no_ring_volume_reports_none_rather_than_zero():
    """None, so the platform can withhold the slider.

    Zero would be a slider claiming the monitor is already silent, which is a
    statement about the hardware that the absent field does not support.
    """
    table = _vto_table()
    del table["Vto00"]["RingVolume"]

    assert (
        vth_camera_links(table, _camera_table())["vtos"]["Vto00"]["ring_volume"] is None
    )


@pytest.mark.parametrize("junk", ["70", 70.5, None, "", [], {}])
def test_a_ring_volume_that_is_not_a_whole_number_reads_as_nothing(junk):
    table = _vto_table(RingVolume=junk)

    assert (
        vth_camera_links(table, _camera_table())["vtos"]["Vto00"]["ring_volume"] is None
    )


@pytest.mark.parametrize("flag", [True, False])
def test_a_boolean_is_not_a_volume(flag):
    """`bool` is an `int` in Python, so `True` would otherwise arrive as volume 1
    and `False` as a monitor that is silent. Neither is a reading."""
    table = _vto_table(RingVolume=flag)

    assert (
        vth_camera_links(table, _camera_table())["vtos"]["Vto00"]["ring_volume"] is None
    )


def test_the_volume_is_read_for_each_door_station_separately():
    """The whole point of the field. A villa's gate and front door are two slots."""
    table = _vto_table()
    table["Vto01"] = {
        "Address": "10.0.0.9",
        "MachineAddress": "Gate",
        "LinkIPC": "",
        "RingVolume": 30,
    }

    vtos = vth_camera_links(table, _camera_table())["vtos"]

    assert {key: slot["ring_volume"] for key, slot in vtos.items()} == {
        "Vto00": 70,
        "Vto01": 30,
    }


def test_an_empty_slot_contributes_no_volume():
    """Twenty slots on the measured monitor, nineteen at 0.0.0.0 and all of them
    carrying `RingVolume: 70`. Without the address gate this device would get
    twenty sliders for one doorbell."""
    table = _vto_table()
    table["Vto05"] = {"Address": "0.0.0.0", "MachineAddress": "", "RingVolume": 70}

    assert list(vth_camera_links(table, _camera_table())["vtos"]) == ["Vto00"]


# --- writing it ----------------------------------------------------------------


async def test_a_write_sends_the_whole_table_with_only_that_volume_changed():
    """The whole table, because a single-field setConfig on a VTH REPLACES it and
    answers result:true while doing so. One field of sixteen survived when that
    was learnt on `VideoTalkPhoneGeneral`, and the monitor had to be rebuilt."""
    device = _Device(_vto_table())
    before = _copy(device.vto_table)

    assert await _client(device).async_set_vth_vto_ring_volume("Vto00", 10) is True

    assert [method for method, _ in device.asked] == [
        "configManager.getConfig",
        "configManager.setConfig",
        "configManager.getConfig",
    ]
    written = device.asked[1][1]
    assert written["name"] == "VTOInfo" and written["options"] == []
    expected = _copy(before)
    expected["Vto00"]["RingVolume"] = 10
    assert written["table"] == expected, "changed more than the one volume"


async def test_the_other_door_stations_keep_their_volume():
    """Named separately from the assertion above, because that one compares a
    table built from the same fixture and would pass if every slot moved
    together with the expectation."""
    table = _vto_table()
    table["Vto01"] = {
        "Address": "10.0.0.9",
        "MachineAddress": "Gate",
        "LinkIPC": "",
        "RingVolume": 30,
    }
    device = _Device(table)

    await _client(device).async_set_vth_vto_ring_volume("Vto00", 10)

    assert device.vto_table["Vto01"]["RingVolume"] == 30


async def test_the_slot_keeps_its_other_fields():
    """Including the VTO's own login. A write that dropped it would take the
    monitor off its door station, which is the failure that made this a
    read-modify-write in the first place."""
    device = _Device(_vto_table("Ipc32"))

    await _client(device).async_set_vth_vto_ring_volume("Vto00", 10)

    slot = device.vto_table["Vto00"]
    assert slot["LinkIPC"] == "Ipc32"
    assert slot["Password"] == "vto-secret"
    assert slot["Address"] == "10.0.0.4"


async def test_a_float_from_the_slider_is_written_as_a_whole_number():
    """Home Assistant hands a Number's callback a float. `70.0` in a config table
    the device stores as an integer is a change of type on every write."""
    device = _Device(_vto_table())

    await _client(device).async_set_vth_vto_ring_volume("Vto00", 10.0)

    assert device.asked[1][1]["table"]["Vto00"]["RingVolume"] == 10
    assert isinstance(device.asked[1][1]["table"]["Vto00"]["RingVolume"], int)


async def test_a_device_that_answers_ok_and_stores_nothing_is_reported_as_refused():
    """#946's shape: a setConfig answered OK and ignored. The readback is the only
    thing that can tell the difference."""
    device = _Device(_vto_table(), applies=False)

    assert await _client(device).async_set_vth_vto_ring_volume("Vto00", 10) is False


async def test_a_slot_the_monitor_does_not_have_is_an_error():
    device = _Device(_vto_table())

    with pytest.raises(ValueError):
        await _client(device).async_set_vth_vto_ring_volume("Vto99", 10)


async def test_setting_the_camera_link_still_works_through_the_shared_writer():
    """⚠️ The two writes share one body now. An absent `LinkIPC` means no camera,
    and that is a per-field rule rather than a general one, so this is here to
    fail if the shared writer stops honouring it."""
    device = _Device(_vto_table(link=None))

    assert await _client(device).async_set_vth_camera_link("Vto00", "") is True


# --- the entity ------------------------------------------------------------------


def _links_coordinator(vto="Vto00", vtos=None, name="Main VTO", volume=70):
    links = {
        "vtos": (
            vtos
            if vtos is not None
            else {vto: {"name": name, "link": "", "ring_volume": volume}}
        ),
        "cameras": {},
    }
    coordinator = object.__new__(DahuaDataUpdateCoordinator)
    coordinator.data = {}
    coordinator.last_update_success = True
    coordinator.get_vth_camera_links = lambda: links
    coordinator.get_serial_number = lambda: "SER123"
    coordinator.get_device_name = lambda: "Hall monitor"
    return coordinator


def _entity(vto="Vto00", vtos=None, name="Main VTO", volume=70):
    coordinator = _links_coordinator(vto, vtos, name, volume)
    entity = object.__new__(DahuaVtoRingVolumeNumber)
    entity._coordinator = coordinator
    entity._vto = vto
    return entity


def test_the_slider_reports_the_measured_range():
    """0 to 100, the same range as the monitor-wide volumes beside it, which is
    what the device reported: 70 on four fields of `VideoTalkPhoneGeneral` and 70
    on every VTOInfo slot.

    ⚠️ Read off an INSTANCE, through the public property. `_attr_native_min_value`
    on the class is the descriptor Home Assistant defines, which is truthy and
    compares equal to nothing, so a class-level assertion here passes vacuously
    (memory: ha-attr-on-the-class-is-a-descriptor).
    """
    entity = _entity()

    assert entity.native_min_value == 0
    assert entity.native_max_value == 100
    assert entity.native_step == 1


def test_the_slider_shows_the_value_for_its_own_door_station():
    both = {
        "Vto00": {"name": "Main VTO", "link": "", "ring_volume": 70},
        "Vto01": {"name": "Gate", "link": "", "ring_volume": 30},
    }

    assert _entity("Vto01", vtos=both).native_value == 30


def test_the_unique_id_names_the_slot_so_two_door_stations_do_not_collide():
    both = {
        "Vto00": {"name": "Main VTO", "link": "", "ring_volume": 70},
        "Vto01": {"name": "Gate", "link": "", "ring_volume": 30},
    }
    first = _entity("Vto00", vtos=both).unique_id
    second = _entity("Vto01", vtos=both).unique_id

    assert first != second
    assert first == "SER123_vto_ring_volume_vto00"


def test_a_slot_that_stops_being_reported_takes_the_slider_unavailable():
    """Rather than leaving the last value on screen as though it were current."""
    entity = _entity(vtos={})

    assert entity.native_value is None
    # `available` reads DahuaBaseEntity's half too, so the field under test is the
    # second term; asserting the whole property here would be asserting the base
    # class's rules as well.
    assert entity._slot().get("ring_volume") is None


def test_the_name_is_a_translation_key_with_the_door_station_in_it():
    """Never `_attr_name`: `DahuaBaseEntity` sets `has_entity_name`, so Home
    Assistant composes DEVICE + ENTITY and the entity says only its own half.

    ⚠️⚠️ Read off an INSTANCE. Home Assistant's entity metaclass turns every
    `_attr_` name into a property, so `DahuaVtoRingVolumeNumber._attr_translation_key`
    is a property object and compares equal to no string at all. Measured on this
    very class: `in vars(C)` is True, `type(C._attr_translation_key)` is
    `property`, and the instance reads the string
    (memory: ha-attr-on-the-class-is-a-descriptor).
    """
    assert _entity().translation_key == "vth_vto_ring_volume"
    # ⚠️ `vars`, not `hasattr`: `Entity` defines `_attr_name`, so `hasattr` on any
    # entity class is True and the assertion would never fire. What is being
    # refused is this class SETTING one.
    assert "_attr_name" not in vars(DahuaVtoRingVolumeNumber)


def test_the_entity_supplies_the_placeholder_its_name_needs():
    """⭐ Derived from the language file rather than both being typed out. The
    English name carries `{vto}`, and a placeholder the entity does not supply
    reaches the user as the literal braces."""
    path = (
        pathlib.Path(__file__).parents[2]
        / "custom_components"
        / "dahua"
        / "translations"
        / "en.json"
    )
    strings = json.loads(path.read_text(encoding="utf-8"))
    name = strings["entity"]["number"]["vth_vto_ring_volume"]["name"]

    wanted = set(re.findall(r"{(\w+)}", name))
    assert wanted, "the name should still carry a placeholder"

    entity = DahuaVtoRingVolumeNumber(
        _links_coordinator(), SimpleNamespace(entry_id="e1"), "Vto00"
    )

    assert set(entity._attr_translation_placeholders) == wanted
    assert entity._attr_translation_placeholders["vto"] == "Main VTO"


def test_an_unnamed_door_station_is_named_by_its_slot():
    """`vth_camera_links` already falls back to the key, so this is the entity
    not undoing it with an empty string."""
    entity = DahuaVtoRingVolumeNumber(
        _links_coordinator(name=""), SimpleNamespace(entry_id="e1"), "Vto00"
    )

    assert entity._attr_translation_placeholders["vto"] == "Vto00"


async def test_a_write_the_monitor_ignores_raises_rather_than_reporting_success():
    entity = _entity()
    entity._coordinator.client = SimpleNamespace(
        async_set_vth_vto_ring_volume=_returns(False)
    )
    entity._coordinator.async_refresh = _returns(None)

    with pytest.raises(HomeAssistantError):
        await entity.async_set_native_value(10)


async def test_a_write_that_lands_asks_for_its_own_slot():
    seen = {}

    async def write(vto, volume):
        seen["call"] = (vto, volume)
        return True

    entity = _entity("Vto01", vtos={"Vto01": {"name": "Gate", "ring_volume": 30}})
    entity._coordinator.client = SimpleNamespace(async_set_vth_vto_ring_volume=write)
    entity._coordinator.async_refresh = _returns(None)

    await entity.async_set_native_value(55)

    assert seen["call"] == ("Vto01", 55)


def _returns(value):
    async def call(*args, **kwargs):
        return value

    return call


# --- which monitors get one --------------------------------------------------------


class _PlatformCoordinator:
    """Only what the number platform reads when it decides."""

    # ⚠️ Setup files its entities under the channel's subentry, so a double that
    # does not carry one fails in `late.py` rather than in the assertion
    # (memory: a-new-requirement-on-setup-breaks-doubles).
    subentry_id = "sub1"

    def __init__(self, monitor=True, links=None, video=False):
        self._monitor = monitor
        self._links = links
        self._video = video
        self.data = {}

    def is_indoor_monitor(self):
        return self._monitor

    def is_indoor_monitor_without_video(self):
        return not self._video

    def get_vth_camera_links(self):
        return self._links

    def get_serial_number(self):
        return "SER123"

    def supports_video_color(self, field):
        return False

    def supports_vth_phone_setting(self, field):
        return False

    def supports_motion_region(self, field):
        return False


async def _added_for(coordinator):
    added = []
    entry = type(
        "E",
        (),
        {
            "entry_id": "e1",
            "runtime_data": {0: coordinator},
            "async_on_unload": staticmethod(lambda unsub: None),
        },
    )()
    await async_setup_entry(SimpleNamespace(data={}), entry, adds_entities(added))
    return [e for e in added if isinstance(e, DahuaVtoRingVolumeNumber)]


async def test_a_monitor_gets_one_slider_per_door_station():
    added = await _added_for(
        _PlatformCoordinator(
            links={
                "vtos": {
                    "Vto00": {"name": "Main VTO", "ring_volume": 70},
                    "Vto01": {"name": "Gate", "ring_volume": 30},
                },
                "cameras": {},
            }
        )
    )

    assert sorted(entity._vto for entity in added) == ["Vto00", "Vto01"]


async def test_a_door_station_with_no_volume_gets_no_slider():
    """The gate. A slot carrying `LinkIPC` and no `RingVolume` would otherwise get
    a slider that can only ever read unknown (#641)."""
    added = await _added_for(
        _PlatformCoordinator(
            links={
                "vtos": {
                    "Vto00": {"name": "Main VTO", "ring_volume": None},
                    "Vto01": {"name": "Gate", "ring_volume": 30},
                },
                "cameras": {},
            }
        )
    )

    assert [entity._vto for entity in added] == ["Vto01"]


async def test_a_camera_gets_none():
    """A recorder channel never has the table, so it gets no sliders.

    ⚠️ The platform's own `is_indoor_monitor()` check is a readability echo
    rather than the protection: `reads_vth_camera_links` below means the data
    is None for anything that is not a monitor, so this passes with that check
    deleted. It was a surviving mutant that said so. The gate that can actually
    fail is tested directly, two tests down.
    """
    assert await _added_for(_PlatformCoordinator(monitor=False)) == []


# --- and the poll fetches what these read ------------------------------------------


def _polled(monitor=True, platforms=(NUMBER,)):
    c = object.__new__(DahuaDataUpdateCoordinator)
    c.is_indoor_monitor = lambda: monitor
    c._wanted_by = lambda *wanted: any(p in platforms for p in wanted)
    return c


def test_the_poll_reads_the_table_for_a_monitor_whose_only_platform_is_number():
    """⭐⭐ THE BUG THIS FEATURE NEARLY SHIPPED WITH. The read was gated on
    `SELECT` alone, because the camera-link select was its only reader. These
    sliders are numbers, so a user who turns the select platform off in the
    entry options would have got controls that are permanently unavailable for
    a table nobody fetches, with nothing in the log to say why."""
    assert _polled(platforms=(NUMBER,)).reads_vth_camera_links() is True


def test_the_select_on_its_own_still_reads_it():
    """The original reader. Widening the gate must not narrow it."""
    assert _polled(platforms=(SELECT,)).reads_vth_camera_links() is True


def test_a_monitor_with_neither_platform_is_not_asked():
    """Nothing reads it, so nothing fetches it. These devices log every request."""
    assert _polled(platforms=()).reads_vth_camera_links() is False


def test_a_device_that_is_not_a_monitor_is_never_asked():
    assert _polled(monitor=False).reads_vth_camera_links() is False


async def test_a_monitor_that_has_not_answered_yet_gets_none():
    """`get_vth_camera_links` is None before the first poll reads it, which is not
    the same as a monitor with no door stations."""
    assert await _added_for(_PlatformCoordinator(links=None)) == []


# --- and the halves agree ------------------------------------------------------------


def test_the_entity_reads_the_key_the_client_writes():
    """Derived rather than spelled twice. The reader puts `ring_volume` on each
    slot and the entity reads it off; a rename in one has to fail here rather than
    produce a slider that is permanently unavailable."""
    links = vth_camera_links(_vto_table(), _camera_table())
    entity = _entity(vtos=links["vtos"])

    assert entity.native_value == 70
