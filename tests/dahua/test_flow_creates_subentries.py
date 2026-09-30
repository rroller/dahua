"""Adding a recorder creates one entry with a channel each, not an entry each.

Ticking sixteen channels used to start sixteen config flows and produce sixteen
config entries, so removing the recorder meant sixteen deletions and a 64 channel
NVR meant 64 (#827). The flow now returns one entry carrying a subentry per
channel, which is the shape Home Assistant expects of a hub.
"""

from types import SimpleNamespace

from custom_components.dahua import channel_configs
from custom_components.dahua.config_flow import CHANNEL_SUBENTRY, DahuaFlowHandler

PRIMARY = {
    "address": "192.168.0.213",
    "channel": 0,
    "name": "Gerty New",
    "username": "admin",
    "password": "secret",
    "port": "80",
    "rtsp_port": "554",
}


def _flow(extra=(), found=None, areas=None, primary=None):
    """A handler far enough along to answer what the final step would create.

    `_channel_subentries` reads no Home Assistant state, so a bare handler is
    enough and this needs no `hass`.
    """
    flow = DahuaFlowHandler()
    flow.init_info = dict(primary or PRIMARY)
    flow._extra_channels = list(extra)
    flow._found_channels = dict(found or {})
    flow._channel_areas = dict(areas or {})
    return flow


# --- what gets created ------------------------------------------------------

def test_a_single_camera_gets_no_subentries_at_all():
    """A camera is not a hub with one member.

    It used to get one, and Home Assistant renders subentry ownership by nesting
    the device underneath -- so a standalone camera read as a recorder that
    happens to have one channel (#830, @roalvesrj). An entry with no subentries
    is the shape `channel_configs()` already handles, and the shape every single
    camera upgrading from 0.9.x already has.
    """
    assert _flow()._channel_subentries() == []


def test_a_recorder_with_one_chosen_channel_still_gets_both():
    """The boundary. One extra channel means two subentries, not one: the primary
    has to be there too or setup brings up every channel except the one the user
    started from."""
    subentries = _flow(extra=[1])._channel_subentries()

    assert [s["data"]["channel"] for s in subentries] == [0, 1]
    assert {s["subentry_type"] for s in subentries} == {CHANNEL_SUBENTRY}


def test_a_recorder_gets_one_channel_each():
    subentries = _flow(extra=[1, 9])._channel_subentries()

    assert [s["data"]["channel"] for s in subentries] == [0, 1, 9]


def test_the_primary_channel_is_a_subentry_too():
    """Not only in the entry's data. Setup reads channels from the subentries
    whenever there are any, so leaving the primary out would bring up every
    channel except the one the user started from."""
    subentries = _flow(extra=[1])._channel_subentries()

    assert 0 in [s["data"]["channel"] for s in subentries]


def test_each_channel_has_its_own_unique_id():
    """Two channels sharing one would collide the moment Home Assistant stored
    them."""
    subentries = _flow(extra=[1, 9])._channel_subentries()

    assert len({s["unique_id"] for s in subentries}) == 3
    assert subentries[0]["unique_id"] == "192.168.0.213_0"


# --- names ------------------------------------------------------------------

def test_extra_channels_are_named_from_what_the_recorder_reported():
    subentries = _flow(extra=[1, 9],
                       found={1: "FRONT VERANDAH", 9: "SIDE YARD"}
                       )._channel_subentries()
    titles = {s["data"]["channel"]: s["title"] for s in subentries}

    assert titles == {0: "Gerty New", 1: "FRONT VERANDAH", 9: "SIDE YARD"}


def test_a_channel_the_recorder_did_not_name_falls_back_to_its_number():
    """One based in the label because that is how the device's own web interface
    counts channels, while the stored value stays zero based."""
    subentries = _flow(extra=[4])._channel_subentries()
    titles = {s["data"]["channel"]: s["title"] for s in subentries}

    assert titles[4] == "Channel 5"


def test_the_primary_keeps_the_name_the_user_typed():
    """It is the only one they were asked about, so the probe must not override
    it."""
    subentries = _flow(extra=[1], found={0: "CH1", 1: "CH2"})._channel_subentries()

    assert subentries[0]["title"] == "Gerty New"


# --- areas, which is where the old code had a trap --------------------------

def test_a_channel_with_no_area_does_not_inherit_the_recorders():
    """Every channel is built from the primary's data, which carries the area the
    user chose for the recorder itself. Without removing it, ticking one area
    would silently file all 64 channels in that one room."""
    subentries = _flow(extra=[1], primary=dict(PRIMARY, area="hallway")
                       )._channel_subentries()
    areas = {s["data"]["channel"]: s["data"].get("area") for s in subentries}

    assert areas[0] == "hallway"
    assert areas[1] is None


def test_a_channel_with_its_own_area_keeps_it():
    subentries = _flow(extra=[1, 9],
                       primary=dict(PRIMARY, area="hallway"),
                       areas={9: "garden"})._channel_subentries()
    areas = {s["data"]["channel"]: s["data"].get("area") for s in subentries}

    assert areas == {0: "hallway", 1: None, 9: "garden"}


# --- and the two halves agree ------------------------------------------------

def test_setup_would_bring_up_every_channel_the_flow_created():
    """The join that matters: what the flow writes is what channel_configs reads.
    These are the two ends of #827 and a disagreement between them would leave
    channels configured but never set up."""
    subentries = _flow(extra=[1, 9])._channel_subentries()
    entry = SimpleNamespace(
        entry_id="e1", data=PRIMARY, options={},
        subentries={s["unique_id"]: SimpleNamespace(data=s["data"])
                    for s in subentries})

    assert [c[1]["channel"] for c in channel_configs(entry)] == [0, 1, 9]
