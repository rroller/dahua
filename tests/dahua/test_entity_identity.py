"""The unique_id of every entity, written down.

A unique_id is a promise. Change one and Home Assistant does not rename the
entity -- it orphans the old registry row and creates a second entity beside
it, so the user loses the name they gave it, the area, the icon, the history
under the old entity_id, and every automation that referred to it.

Nothing in this integration declares these strings. Each one is assembled at
runtime from the serial number, the channel index and a per-entity suffix, in
five separate modules. This file is the only place they exist as literals, so
that a change to how they are assembled has to be a change here too.

The channels are not arbitrary: 0 is the single-camera case and the one with
the irregular rule, and 1, 2 and 11 are NVR channels, 11 being the far end of
a real eleven-channel install.
"""

import pytest

from custom_components.dahua import DahuaDataUpdateCoordinator
from custom_components.dahua import binary_sensor as bs_module
from custom_components.dahua import camera as camera_module
from custom_components.dahua import light as light_module
from custom_components.dahua import select as select_module
from custom_components.dahua import switch as switch_module
from custom_components.dahua.binary_sensor import DahuaEventSensor
from custom_components.dahua.camera import DahuaCamera
from custom_components.dahua.entity import DahuaBaseEntity
from custom_components.dahua.light import (
    AmcrestRingLight,
    DahuaIlluminator,
    DahuaInfraredLight,
    DahuaSecurityLight,
    FloodLight,
)
from custom_components.dahua.select import (
    DahuaCameraPresetPositionSelect,
    DahuaDoorbellLightSelect,
)
from custom_components.dahua.switch import (
    DahuaDisarmingEventNotificationsLinkageBinarySwitch,
    DahuaDisarmingLinkageBinarySwitch,
    DahuaMotionDetectionBinarySwitch,
    DahuaSirenBinarySwitch,
    DahuaSmartMotionDetectionBinarySwitch,
)

SERIAL = "4L03CB4PAZC9E8F"

CHANNELS = [0, 1, 2, 11]

# The serial the coordinator reports for a channel. Channel 0 is bare, and it
# has to stay bare: every single-camera install ever set up has entities keyed
# on it with no suffix.
GOLDEN_SERIALS = {
    0: "4L03CB4PAZC9E8F",
    1: "4L03CB4PAZC9E8F_1",
    2: "4L03CB4PAZC9E8F_2",
    11: "4L03CB4PAZC9E8F_11",
}


# --- the coordinator's half -------------------------------------------------

@pytest.mark.parametrize("channel", CHANNELS)
def test_the_serial_a_channel_reports(channel):
    coordinator = object.__new__(DahuaDataUpdateCoordinator)
    coordinator._channel = channel
    coordinator._serial_number = SERIAL

    assert coordinator.get_serial_number() == GOLDEN_SERIALS[channel]


def test_channel_zero_is_bare_and_the_others_are_not():
    """Stated on its own because it is the irregularity, not a typo."""
    assert GOLDEN_SERIALS[0] == SERIAL
    assert all(GOLDEN_SERIALS[c] != SERIAL for c in CHANNELS if c != 0)


# --- building real entities -------------------------------------------------

class _Client:
    @staticmethod
    def to_stream_name(subtype):
        if subtype == 0:
            return "Main"
        if subtype == 1:
            return "Sub"
        return "Sub" + str(subtype)

    @staticmethod
    def get_rtsp_stream_url(channel, subtype):
        return "rtsp://host/cam?channel=%s&subtype=%s" % (channel, subtype)


class _Coordinator:
    """Reports identity through the real coordinator's own method."""

    def __init__(self, channel):
        self._channel = channel
        self._serial_number = SERIAL
        self._channel_number = channel + 1
        self.client = _Client()

    get_serial_number = DahuaDataUpdateCoordinator.get_serial_number

    def get_channel(self):
        return self._channel

    def get_channel_number(self):
        return self._channel_number

    def get_device_name(self):
        return "Front Street"


class _Entry:
    title = "Front Street"


@pytest.fixture(autouse=True)
def _skip_ha_plumbing(monkeypatch):
    """Build the real entities, skipping only Home Assistant's own __init__."""
    for module in (bs_module, camera_module, light_module, select_module, switch_module):
        monkeypatch.setattr(module.DahuaBaseEntity, "__init__", lambda self, c, e: None)
    monkeypatch.setattr(bs_module.BinarySensorEntity, "__init__", lambda self: None)
    monkeypatch.setattr(select_module.SelectEntity, "__init__", lambda self: None)
    monkeypatch.setattr(camera_module.Camera, "__init__", lambda self: None)


def _bare(cls, coordinator):
    """A class whose id comes from the base entity and takes no constructor."""
    entity = object.__new__(cls)
    entity._coordinator = coordinator
    return entity


# Every unique_id this integration produces is a golden serial above followed
# by exactly one of these. The builder is what the platform module actually
# calls, so a constructor that starts computing the suffix differently is
# caught here.
GOLDEN_SUFFIXES = [
    # binary_sensor.py -- one per event the camera reports
    ("", lambda c: DahuaEventSensor(c, _Entry(), "VideoMotion")),
    ("_cross_line_alarm", lambda c: DahuaEventSensor(c, _Entry(), "CrossLineDetection")),
    ("_smart_motion_human", lambda c: DahuaEventSensor(c, _Entry(), "SmartMotionHuman")),
    ("_smart_motion_vehicle", lambda c: DahuaEventSensor(c, _Entry(), "SmartMotionVehicle")),
    ("_alarm_local", lambda c: DahuaEventSensor(c, _Entry(), "AlarmLocal")),
    ("_button_pressed", lambda c: DahuaEventSensor(c, _Entry(), "DoorbellPressed")),
    # camera.py -- one per stream
    ("_Main", lambda c: DahuaCamera(c, 0, _Entry())),
    ("_Sub", lambda c: DahuaCamera(c, 1, _Entry())),
    ("_Sub2", lambda c: DahuaCamera(c, 2, _Entry())),
    # switch.py
    ("_motion_detection", lambda c: _bare(DahuaMotionDetectionBinarySwitch, c)),
    ("_disarming", lambda c: _bare(DahuaDisarmingLinkageBinarySwitch, c)),
    ("_event_notifications",
     lambda c: _bare(DahuaDisarmingEventNotificationsLinkageBinarySwitch, c)),
    ("_smart_motion_detection", lambda c: _bare(DahuaSmartMotionDetectionBinarySwitch, c)),
    ("_siren", lambda c: _bare(DahuaSirenBinarySwitch, c)),
    # light.py
    ("_infrared", lambda c: DahuaInfraredLight(c, _Entry(), "Infrared")),
    ("_illuminator", lambda c: DahuaIlluminator(c, _Entry(), "Illuminator")),
    ("_ring_light", lambda c: AmcrestRingLight(c, _Entry(), "Ring Light")),
    ("_flood_light", lambda c: FloodLight(c, _Entry(), "Flood Light")),
    ("_security", lambda c: DahuaSecurityLight(c, _Entry(), "Security")),
    # select.py
    ("_security_light", lambda c: DahuaDoorbellLightSelect(c, _Entry())),
    ("_preset_position", lambda c: DahuaCameraPresetPositionSelect(c, _Entry())),
    ("_1_preset_position",
     lambda c: DahuaCameraPresetPositionSelect(c, _Entry(), rpc2_channel=1)),
]


@pytest.mark.parametrize("channel", CHANNELS)
@pytest.mark.parametrize("suffix,build", GOLDEN_SUFFIXES, ids=[s or "bare" for s, _ in GOLDEN_SUFFIXES])
def test_the_unique_id_of_every_entity(channel, suffix, build):
    entity = build(_Coordinator(channel))

    assert entity.unique_id == GOLDEN_SERIALS[channel] + suffix


def test_the_whole_set_for_one_nvr_channel_spelled_out():
    """Channel 2, in full, with nothing composed.

    If the table above is ever regenerated from the code it is meant to be
    checking, this is what still says what the strings are.
    """
    ids = {build(_Coordinator(2)).unique_id for _, build in GOLDEN_SUFFIXES}

    assert ids == {
        "4L03CB4PAZC9E8F_2",
        "4L03CB4PAZC9E8F_2_cross_line_alarm",
        "4L03CB4PAZC9E8F_2_smart_motion_human",
        "4L03CB4PAZC9E8F_2_smart_motion_vehicle",
        "4L03CB4PAZC9E8F_2_alarm_local",
        "4L03CB4PAZC9E8F_2_button_pressed",
        "4L03CB4PAZC9E8F_2_Main",
        "4L03CB4PAZC9E8F_2_Sub",
        "4L03CB4PAZC9E8F_2_Sub2",
        "4L03CB4PAZC9E8F_2_motion_detection",
        "4L03CB4PAZC9E8F_2_disarming",
        "4L03CB4PAZC9E8F_2_event_notifications",
        "4L03CB4PAZC9E8F_2_smart_motion_detection",
        "4L03CB4PAZC9E8F_2_siren",
        "4L03CB4PAZC9E8F_2_infrared",
        "4L03CB4PAZC9E8F_2_illuminator",
        "4L03CB4PAZC9E8F_2_ring_light",
        "4L03CB4PAZC9E8F_2_flood_light",
        "4L03CB4PAZC9E8F_2_security",
        "4L03CB4PAZC9E8F_2_security_light",
        "4L03CB4PAZC9E8F_2_preset_position",
        "4L03CB4PAZC9E8F_2_1_preset_position",
    }


def test_the_base_entity_falls_back_to_the_bare_serial():
    """An entity that declares no suffix of its own gets the device's id."""
    entity = object.__new__(DahuaBaseEntity)
    entity._coordinator = _Coordinator(2)

    assert entity.unique_id == "4L03CB4PAZC9E8F_2"


# --- collisions -------------------------------------------------------------

# select.py picks exactly one preset select per entry: the rpc2 one for an
# SDT4E425, the plain one otherwise. They cannot both exist, which is the only
# reason the pair below does not collide.
EXCLUSIVE = "_1_preset_position"
COEXISTING = [(suffix, build) for suffix, build in GOLDEN_SUFFIXES if suffix != EXCLUSIVE]


@pytest.mark.parametrize("channel", CHANNELS)
def test_no_two_entities_on_a_channel_share_an_id(channel):
    ids = [build(_Coordinator(channel)).unique_id for _, build in COEXISTING]

    assert len(set(ids)) == len(ids)


def test_no_two_channels_share_an_id():
    """The whole reason the channel is in the serial at all."""
    ids = [build(_Coordinator(channel)).unique_id
           for channel in CHANNELS for _, build in COEXISTING]

    assert len(set(ids)) == len(ids)


def test_the_rpc2_preset_select_reads_as_channel_ones_plain_one():
    """The near miss, written down so it is a decision and not an accident.

    The suffix "1_preset_position" puts a "1" exactly where the channel
    separator goes, so channel 0's rpc2 select and channel 1's plain select
    both come out as SERIAL_1_preset_position.

    They never coexist -- select.py creates the rpc2 one only for an SDT4E425
    and the plain one only for everything else, and two entries against one
    serial are one device and so one model. Making that branch anything other
    than mutually exclusive would make this a live collision.
    """
    rpc2_on_channel_zero = DahuaCameraPresetPositionSelect(
        _Coordinator(0), _Entry(), rpc2_channel=1
    ).unique_id
    plain_on_channel_one = DahuaCameraPresetPositionSelect(
        _Coordinator(1), _Entry()
    ).unique_id

    assert rpc2_on_channel_zero == plain_on_channel_one == "4L03CB4PAZC9E8F_1_preset_position"


def test_a_channel_id_is_not_a_prefix_of_a_different_entity_on_channel_zero():
    """Channel 1's bare id must not read as a suffixed id on channel 0.

    "SERIAL_1" is the motion sensor for channel 1. Any entity on channel 0
    whose suffix happened to be "1" would take the same string.
    """
    channel_one = _Coordinator(1).get_serial_number()
    channel_zero_ids = {build(_Coordinator(0)).unique_id for _, build in GOLDEN_SUFFIXES}

    assert channel_one not in channel_zero_ids
