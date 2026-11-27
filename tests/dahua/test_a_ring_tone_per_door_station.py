"""A ring tone per door station, on an indoor monitor whose tones have been heard.

`VTOInfo.VtoNN.RingFile` sits beside the ring volume in the same slot. It is a
path on the monitor, and the monitor stores whatever it is given.

**Measured 2026-11-27, by ear, on two DHI-VTH5221D (3.000.0012000.0.R)** with
the owner pressing a VTO2000A's button after each change, one monitor changed
and the other left alone as the comparison:

    phone_ring1.pcm   the factory tone, held by all twenty slots of both
    phone_ring2.pcm   a telephone ring, on the monitor it was set on only
    phone_ring3.pcm   accepted, read back intact, and the monitor was silent
    phone_ring4, 5 and 6   the same: stored, and silent

Each write was the whole table, after a write of the unchanged table had been
read back identical, and exactly one field differed afterwards.

So the select offers the two that play and nothing else, on that model and no
other, and the client refuses a path it has not heard. The failure being
guarded against is not an error anybody sees: it is a doorbell that rings in
silence while Home Assistant shows the setting as applied.

⚠ Not measured: any other monitor model. `system.listService` on the VTH5221D
has no file service, so there is no list to read on any of them.
"""

import json
import pathlib
from types import SimpleNamespace

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.dahua import client as client_module
from custom_components.dahua.client import (
    VTH_RING_TONES,
    vth_camera_links,
    vth_ring_tones,
)
from custom_components.dahua.select import (
    DahuaVtoRingToneSelect,
    vto_ring_tone_selects,
)

from .test_vth_camera_link import _camera_table, _client, _copy, _Device, _vto_table

RING_1 = "/mnt/data/Sounds/phoneRing/phone_ring1.pcm"
RING_2 = "/mnt/data/Sounds/phoneRing/phone_ring2.pcm"
RING_3 = "/mnt/data/Sounds/phoneRing/phone_ring3.pcm"


# --- which tones, and for which monitor -------------------------------------------------


def test_the_two_tones_that_were_heard_are_the_two_offered():
    assert vth_ring_tones("VTH5221D") == {"ring_1": RING_1, "ring_2": RING_2}


@pytest.mark.parametrize("model", ["DHI-VTH5221D", "vth5221d", "VTH5221D-S2"])
def test_the_model_is_recognised_however_the_device_spells_it(model):
    assert vth_ring_tones(model) == {"ring_1": RING_1, "ring_2": RING_2}


@pytest.mark.parametrize(
    "model", ["VTH2421F-P", "VTH8A21KMS-W", "VTO2000A", "IPC-HDW3849H", "", None]
)
def test_a_monitor_nobody_has_listened_to_is_offered_nothing(model):
    """It may carry the same files. Guessing wrong is a silent doorbell."""
    assert vth_ring_tones(model) == {}


def test_the_table_is_not_handed_out_to_be_edited():
    vth_ring_tones("VTH5221D")["ring_9"] = "/somewhere/else.pcm"

    assert "ring_9" not in VTH_RING_TONES["VTH5221D"]


@pytest.mark.parametrize("silent", [3, 4, 5, 6])
def test_the_tones_that_were_silent_are_not_offered(silent):
    path = "/mnt/data/Sounds/phoneRing/phone_ring%d.pcm" % silent

    assert path not in vth_ring_tones("VTH5221D").values()


# --- reading it off the slot ------------------------------------------------------------


def test_the_slot_reports_the_file_it_rings_with():
    links = vth_camera_links(_vto_table(RingFile=RING_1), _camera_table())

    assert links["vtos"]["Vto00"]["ring_file"] == RING_1


def test_a_slot_with_no_such_field_reports_none():
    assert (
        vth_camera_links(_vto_table(), _camera_table())["vtos"]["Vto00"]["ring_file"]
        is None
    )


@pytest.mark.parametrize("junk", ["", None, 1, True, [], {}])
def test_a_ring_file_that_is_not_a_path_reads_as_nothing(junk):
    links = vth_camera_links(_vto_table(RingFile=junk), _camera_table())

    assert links["vtos"]["Vto00"]["ring_file"] is None


# --- writing it -------------------------------------------------------------------------


async def test_a_write_sends_the_whole_table_with_only_that_tone_changed():
    """The whole table, because a one-field setConfig REPLACES it on a VTH."""
    device = _Device(_vto_table(RingFile=RING_1))
    before = _copy(device.vto_table)

    assert await _client(device).async_set_vth_vto_ring_tone("Vto00", RING_2) is True

    assert [method for method, _ in device.asked] == [
        "configManager.getConfig",
        "configManager.setConfig",
        "configManager.getConfig",
    ]
    expected = _copy(before)
    expected["Vto00"]["RingFile"] = RING_2
    assert device.vto_table == expected


async def test_the_volume_and_the_login_in_the_slot_are_left_alone():
    device = _Device(_vto_table(RingFile=RING_1))

    await _client(device).async_set_vth_vto_ring_tone("Vto00", RING_2)

    slot = device.vto_table["Vto00"]
    assert slot["RingVolume"] == 70
    assert slot["Password"] == "vto-secret"
    assert slot["Address"] == "10.0.0.4"


@pytest.mark.parametrize(
    "path",
    [
        RING_3,
        "/mnt/data/Sounds/phoneRing/phone_ring6.pcm",
        "/mnt/sd/mine.pcm",
        "phone_ring2.pcm",
        "",
        None,
    ],
)
async def test_a_file_nobody_has_heard_is_not_sent(path):
    """The monitor would take it, answer `result: true`, read it back intact
    and ring in silence. Nothing downstream can tell that from success, so it
    stops here."""
    device = _Device(_vto_table(RingFile=RING_1))
    before = _copy(device.vto_table)

    with pytest.raises(ValueError):
        await _client(device).async_set_vth_vto_ring_tone("Vto00", path)

    assert device.asked == []
    assert device.vto_table == before


async def test_a_monitor_that_answers_ok_and_stores_nothing_is_reported():
    device = _Device(_vto_table(RingFile=RING_1), applies=False)

    assert await _client(device).async_set_vth_vto_ring_tone("Vto00", RING_2) is False


# --- the entity ---------------------------------------------------------------------------


class _Coordinator:
    """Only what the select reads. The model is what decides the options."""

    def __init__(self, links=None, model="VTH5221D", monitor=True, landed=True):
        self._links = links
        self.model = model
        self._monitor = monitor
        self.refreshed = 0
        self.asked = []
        self.client = SimpleNamespace(async_set_vth_vto_ring_tone=self._write)
        self._landed = landed

    async def _write(self, vto, path):
        self.asked.append((vto, path))
        return self._landed

    def is_indoor_monitor(self):
        return self._monitor

    def get_model(self):
        return self.model

    def get_vth_camera_links(self):
        return self._links

    # Read by the base classes for `available`, which every entity answers.
    last_update_success = True

    def get_address(self):
        return "10.0.0.7"

    def get_serial_number(self):
        return "SERIAL7"

    def get_device_name(self):
        return "Hall monitor"

    async def async_refresh(self):
        self.refreshed += 1


def _links(ring_file=RING_1, **more):
    vtos = {"Vto00": {"name": "Main VTO", "ring_volume": 70, "ring_file": ring_file}}
    vtos.update(more)
    return {"vtos": vtos, "cameras": {}}


@pytest.fixture
def select(monkeypatch):
    """The real select, without Home Assistant's entity plumbing."""
    from custom_components.dahua import select as select_module

    monkeypatch.setattr(
        select_module.DahuaBaseEntity, "__init__", lambda self, c, e: None
    )
    monkeypatch.setattr(select_module.DahuaBaseEntity, "available", True)

    def build(coordinator, vto="Vto00"):
        return DahuaVtoRingToneSelect(coordinator, object(), vto)

    return build


def test_the_options_are_the_tones_heard_on_this_model(select):
    assert select(_Coordinator(_links())).options == ["ring_1", "ring_2"]


@pytest.mark.parametrize("held, option", [(RING_1, "ring_1"), (RING_2, "ring_2")])
def test_it_shows_the_tone_the_monitor_holds(select, held, option):
    assert select(_Coordinator(_links(held))).current_option == option


def test_a_tone_it_does_not_know_shows_as_unknown_not_as_the_first_option(select):
    """Something is set, and it is not one of these. Showing "Ring 1" would
    say the doorbell is on its default when it may be silent."""
    assert select(_Coordinator(_links(RING_3))).current_option is None


async def test_choosing_a_tone_writes_its_file_for_its_own_door_station(select):
    coordinator = _Coordinator(_links())

    await select(coordinator).async_select_option("ring_2")

    assert coordinator.asked == [("Vto00", RING_2)]
    assert coordinator.refreshed == 1


async def test_an_option_that_is_not_one_of_them_writes_nothing(select):
    coordinator = _Coordinator(_links())

    await select(coordinator).async_select_option("ring_3")

    assert coordinator.asked == []


async def test_a_write_the_monitor_ignores_raises_rather_than_looking_applied(select):
    coordinator = _Coordinator(_links(), landed=False)

    with pytest.raises(HomeAssistantError) as raised:
        await select(coordinator).async_select_option("ring_2")

    assert raised.value.translation_key == "vth_vto_ring_tone_ignored"
    assert raised.value.translation_placeholders == {
        "device": "Hall monitor",
        "vto": "Main VTO",
    }
    assert coordinator.refreshed == 1, "the state shown is the monitor's, re-read"


def test_the_unique_id_names_the_slot(select):
    coordinator = _Coordinator(
        _links(Vto01={"name": "Gate", "ring_volume": 30, "ring_file": RING_2})
    )

    assert select(coordinator, "Vto00").unique_id == "SERIAL7_vto_ring_tone_vto00"
    assert select(coordinator, "Vto01").unique_id == "SERIAL7_vto_ring_tone_vto01"


def test_a_slot_that_stops_being_reported_takes_it_unavailable(select):
    coordinator = _Coordinator(_links())
    entity = select(coordinator)
    assert entity.available is True

    coordinator._links = {"vtos": {}, "cameras": {}}

    assert entity.available is False


def test_the_name_carries_the_door_station(select):
    entity = select(_Coordinator(_links()))

    assert entity._attr_translation_key == "vth_vto_ring_tone"
    assert entity._attr_translation_placeholders == {"vto": "Main VTO"}


def test_every_option_has_a_label_and_the_name_has_its_placeholder():
    """An option with no string shows as its slug, and a name whose
    placeholder is not supplied does not render at all."""
    strings = json.loads(
        (
            pathlib.Path(client_module.__file__).parent / "translations" / "en.json"
        ).read_text(encoding="utf-8")
    )
    entry = strings["entity"]["select"]["vth_vto_ring_tone"]

    assert "{vto}" in entry["name"]
    for tones in VTH_RING_TONES.values():
        assert set(tones) <= set(entry["state"])
    assert "{device}" in strings["exceptions"]["vth_vto_ring_tone_ignored"]["message"]
    assert "{vto}" in strings["exceptions"]["vth_vto_ring_tone_ignored"]["message"]


# --- who gets one -------------------------------------------------------------------------


def test_a_monitor_gets_one_per_door_station():
    coordinator = _Coordinator(
        _links(Vto01={"name": "Gate", "ring_volume": 30, "ring_file": RING_2})
    )

    made = vto_ring_tone_selects(coordinator, object())

    assert sorted(entity._vto for entity in made) == ["Vto00", "Vto01"]


def test_a_door_station_whose_slot_has_no_ring_file_gets_none():
    coordinator = _Coordinator(_links(ring_file=None))

    assert vto_ring_tone_selects(coordinator, object()) == []


def test_a_monitor_of_a_model_nobody_has_listened_to_gets_none():
    """Even though its slot carries the very same path."""
    coordinator = _Coordinator(_links(), model="VTH2421F-P")

    assert vto_ring_tone_selects(coordinator, object()) == []


def test_a_device_that_is_not_a_monitor_gets_none():
    coordinator = _Coordinator(_links(), monitor=False)

    assert vto_ring_tone_selects(coordinator, object()) == []


def test_a_monitor_that_has_not_answered_yet_gets_none():
    assert vto_ring_tone_selects(_Coordinator(None), object()) == []
