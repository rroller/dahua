"""binary_sensor.py had no tests. Its naming and IDs are derived, not declared."""

import pytest

from custom_components.dahua import binary_sensor as bs
from custom_components.dahua.binary_sensor import DahuaEventSensor
from custom_components.dahua.const import (
    DOOR_DEVICE_CLASS,
    MOTION_SENSOR_DEVICE_CLASS,
    SAFETY_DEVICE_CLASS,
    SOUND_DEVICE_CLASS,
    VOLUME_HIGH_ICON,
)


class _Coordinator:
    def __init__(self):
        self.timestamps = {}
        self.listeners = []

    def get_serial_number(self):
        return "SERIAL1"

    def get_device_name(self):
        return "Front Door"

    def get_event_timestamp(self, event_name):
        return self.timestamps.get(event_name, 0)

    def add_dahua_event_listener(self, event_name, callback):
        self.listeners.append((event_name, callback))


@pytest.fixture
def sensor(monkeypatch):
    """Build real sensors, skipping only Home Assistant's entity plumbing."""
    monkeypatch.setattr(bs.DahuaBaseEntity, "__init__", lambda self, c, e: None)
    monkeypatch.setattr(bs.BinarySensorEntity, "__init__", lambda self: None)

    def build(event_name, coordinator=None):
        return DahuaEventSensor(coordinator or _Coordinator(), object(), event_name)

    return build


# --- names are derived from the event code ---------------------------------

@pytest.mark.parametrize("event_name,expected", [
    ("SmartMotionHuman", "Smart Motion Human"),
    ("SmartMotionVehicle", "Smart Motion Vehicle"),
    ("CrossRegionDetection", "Cross Region Detection"),
    ("AudioMutation", "Audio Mutation"),
    ("AlarmLocal", "Alarm Local"),
    ("StorageNotExist", "Storage Not Exist"),
])
def test_camel_case_events_become_readable_names(sensor, event_name, expected):
    assert sensor(event_name).name == "Front Door " + expected


@pytest.mark.parametrize("event_name,expected", [
    ("VideoMotion", "Motion Alarm"),
    ("CrossLineDetection", "Cross Line Alarm"),
    ("DoorbellPressed", "Button Pressed"),
])
def test_overridden_names_win_over_the_derived_one(sensor, event_name, expected):
    assert sensor(event_name).name == "Front Door " + expected


def test_consecutive_capitals_are_not_split(sensor):
    """IVS must not become I V S."""
    assert sensor("IVS").name == "Front Door IVS"


# --- device classes and icons ----------------------------------------------

@pytest.mark.parametrize("event_name,expected", [
    ("VideoMotion", MOTION_SENSOR_DEVICE_CLASS),
    ("AlarmLocal", SAFETY_DEVICE_CLASS),
    ("VideoLoss", SAFETY_DEVICE_CLASS),
    ("DoorStatus", DOOR_DEVICE_CLASS),
    ("AudioMutation", SOUND_DEVICE_CLASS),
    ("SmartMotionHuman", MOTION_SENSOR_DEVICE_CLASS),  # the fallback
])
def test_device_class_mapping(sensor, event_name, expected):
    assert sensor(event_name).device_class == expected


def test_audio_events_get_the_volume_icon(sensor):
    assert sensor("AudioMutation").icon == VOLUME_HIGH_ICON
    assert sensor("VideoMotion").icon is None


# --- identity, including a back-compat case that must not be tidied away ---

def test_video_motion_keeps_the_bare_serial_as_its_id(sensor):
    """Changing this orphans every existing motion sensor on upgrade."""
    assert sensor("VideoMotion").unique_id == "SERIAL1"


def test_other_events_get_a_suffixed_id(sensor):
    assert sensor("SmartMotionHuman").unique_id == "SERIAL1_smart_motion_human"
    assert sensor("CrossLineDetection").unique_id == "SERIAL1_cross_line_alarm"


def test_ids_are_distinct_across_the_events_a_camera_reports(sensor):
    events = ["VideoMotion", "CrossLineDetection", "AlarmLocal", "VideoLoss",
              "VideoBlind", "AudioMutation", "CrossRegionDetection",
              "SmartMotionHuman", "SmartMotionVehicle"]
    ids = [sensor(e).unique_id for e in events]

    assert len(set(ids)) == len(ids), "two events would share one entity: %s" % ids


# --- state comes from the event stream, not polling ------------------------

def test_is_on_follows_the_event_timestamp(sensor):
    c = _Coordinator()
    s = sensor("SmartMotionHuman", c)

    assert s.is_on is False
    c.timestamps["SmartMotionHuman"] = 1_700_000_000
    assert s.is_on is True
    c.timestamps["SmartMotionHuman"] = 0
    assert s.is_on is False


def test_each_sensor_only_watches_its_own_event(sensor):
    c = _Coordinator()
    s = sensor("SmartMotionHuman", c)
    c.timestamps["VideoMotion"] = 1_700_000_000

    assert s.is_on is False, "reacted to a different event"


async def test_it_subscribes_to_its_event_when_added(sensor):
    c = _Coordinator()
    s = sensor("CrossLineDetection", c)

    await s.async_added_to_hass()

    assert [name for name, _ in c.listeners] == ["CrossLineDetection"]


def test_these_sensors_are_pushed_not_polled(sensor):
    assert sensor("VideoMotion").should_poll is False
