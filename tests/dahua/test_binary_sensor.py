import time
import pytest

from custom_components.dahua import binary_sensor as bs
from custom_components.dahua.coordinator import DahuaDataUpdateCoordinator
from custom_components.dahua.binary_sensor import (
    DahuaEventSensor,
    DahuaAuthorizedVehicleBinarySensor,
)
from custom_components.dahua.const import (
    DOOR_DEVICE_CLASS,
    MOTION_SENSOR_DEVICE_CLASS,
    SAFETY_DEVICE_CLASS,
    SOUND_DEVICE_CLASS,
    VOLUME_HIGH_ICON,
)


class _Coordinator:
    # The real thing, not a stand-in. Both of these return the callback that
    # undoes them, the entities hand that to `async_on_remove`, and a fake
    # returning None would have been accepted without a word.
    add_dahua_event_listener = DahuaDataUpdateCoordinator.add_dahua_event_listener
    add_plate_listener = DahuaDataUpdateCoordinator.add_plate_listener
    get_event_key = DahuaDataUpdateCoordinator.get_event_key

    def __init__(self):
        self.timestamps = {}
        self._channel = 0
        self._dahua_event_listeners = {}
        self._plate_listeners = []
        self._last_plate = "unknown"
        self._last_plate_data = {}
        self._last_plate_timestamp = 0
        self._authorized_plates = ["ABC1234", "XYZ5678"]
        self._authorized_hold_time = 60

    def get_serial_number(self):
        return "SERIAL1"

    def get_device_name(self):
        return "Front Door"

    def get_event_timestamp(self, event_name):
        return self.timestamps.get(event_name, 0)

    def event_is_momentary(self, event_name):
        """No event here has arrived as a Pulse, so none clears itself."""
        return False

    def get_last_plate(self):
        return self._last_plate

    def get_last_plate_data(self):
        return self._last_plate_data

    def get_last_plate_timestamp(self):
        return self._last_plate_timestamp

    def get_authorized_plates(self):
        return self._authorized_plates

    def get_authorized_hold_time(self):
        return self._authorized_hold_time

    def is_plate_authorized(self, plate):
        return plate in self._authorized_plates



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
    # `has_entity_name` is True, so an entity states only its own half and
    # Home Assistant prefixes the device. The rendered name is unchanged;
    # what this asserts is that the entity is no longer composing it.
    assert sensor(event_name).name == expected


@pytest.mark.parametrize("event_name,expected", [
    ("VideoMotion", "Motion Alarm"),
    ("CrossLineDetection", "Cross Line Alarm"),
    ("DoorbellPressed", "Button Pressed"),
])
def test_overridden_names_win_over_the_derived_one(sensor, event_name, expected):
    # `has_entity_name` is True, so an entity states only its own half and
    # Home Assistant prefixes the device. The rendered name is unchanged;
    # what this asserts is that the entity is no longer composing it.
    assert sensor(event_name).name == expected


def test_consecutive_capitals_are_not_split(sensor):
    """IVS must not become I V S."""
    # `has_entity_name` is True, so an entity states only its own half and
    # Home Assistant prefixes the device. The rendered name is unchanged;
    # what this asserts is that the entity is no longer composing it.
    assert sensor("IVS").name == "IVS"


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

    assert list(c._dahua_event_listeners) == [c.get_event_key("CrossLineDetection")]


async def test_it_stops_listening_when_it_is_removed(sensor):
    """A removed entity that keeps its callback is not only untidy. Whether a
    key has listeners decides whether `_dispatch_event` reports that code at
    all, and whether `translate_event_code` reports CrossLineDetection
    alongside the SmartMotion translation, so a ghost answers yes to both. And
    nothing stops the callback reaching an entity Home Assistant has removed.

    `_on_remove` is Home Assistant's own list, which it drains on removal.
    Reaching into it is how this test removes the entity without a platform.
    """
    c = _Coordinator()
    s = sensor("CrossLineDetection", c)

    await s.async_added_to_hass()
    assert c._dahua_event_listeners, "never subscribed, so this proves nothing"

    assert s._on_remove, "registered nothing to undo the subscription"
    for undo in list(s._on_remove):
        undo()

    assert c._dahua_event_listeners == {}, (
        "the key outlived the entity, so the poll still thinks something reads it")


def test_these_sensors_are_pushed_not_polled(sensor):
    assert sensor("VideoMotion").should_poll is False


# --- authorized vehicle sensor tests ---------------------------------------

def test_authorized_vehicle_sensor_properties():
    c = _Coordinator()
    s = DahuaAuthorizedVehicleBinarySensor(c, object())

    # The name itself lives in translations/en.json now, and is pinned there
    # by test_entity_names_come_from_translations.py. The key is what this
    # entity is responsible for.
    assert s.translation_key == "authorized_vehicle"
    assert s.unique_id == "SERIAL1_authorized_vehicle"
    assert s.device_class == "presence"
    # The icon moved to icons.json with the name, and asserting it is None here
    # rather than dropping the line: it has to *stay* gone, because
    # `Entity.icon` returns `_attr_icon` whenever it is set and would then win
    # over the file silently. mdi:car-check is pinned in
    # test_entity_icons_come_from_icons_json.py.
    assert s.icon is None
    assert s.should_poll is False


def test_authorized_vehicle_sensor_state_and_attributes():
    c = _Coordinator()
    s = DahuaAuthorizedVehicleBinarySensor(c, object())

    # Initially off
    assert s.is_on is False

    # Unauthorized vehicle detected
    c._last_plate = "UNKNOWN99"
    c._last_plate_timestamp = int(time.time())
    assert s.is_on is False

    # Authorized vehicle detected within hold time
    c._last_plate = "ABC1234"
    c._last_plate_data = {
        "plate": "ABC1234",
        "vehicle_brand": "Volkswagen",
        "vehicle_color": "Black",
        "vehicle_type": "SUV",
        "direction": "Approach",
    }
    c._last_plate_timestamp = int(time.time())
    assert s.is_on is True

    # Check attributes
    attrs = s.extra_state_attributes
    assert attrs["authorized_plates"] == ["ABC1234", "XYZ5678"]
    assert attrs["hold_time_seconds"] == 60
    assert attrs["last_matched_brand"] == "Volkswagen"
    assert attrs["last_matched_color"] == "Black"
    assert attrs["last_matched_type"] == "SUV"
    assert attrs["direction"] == "Approach"

    # Expired hold time
    c._last_plate_timestamp = int(time.time()) - 120
    assert s.is_on is False

