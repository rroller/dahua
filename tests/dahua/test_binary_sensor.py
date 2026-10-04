import time
import pytest

from custom_components.dahua import binary_sensor as bs
from custom_components.dahua.coordinator import DahuaDataUpdateCoordinator
from custom_components.dahua.binary_sensor import (
    DEFAULT_PULSE_HOLD_SECONDS,
    MOMENTARY_EVENT_HOLD_SECONDS,
    DahuaEventSensor,
    DahuaAuthorizedVehicleBinarySensor,
    DahuaDiskProblemBinarySensor,
)
from custom_components.dahua.const import (
    DOOR_DEVICE_CLASS,
    MOTION_SENSOR_DEVICE_CLASS,
    SAFETY_DEVICE_CLASS,
    SOUND_DEVICE_CLASS,
    TAMPER_DEVICE_CLASS,
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
        # DahuaBaseEntity.extra_state_attributes reads data.get("id"), and the
        # authorized vehicle sensor adds to that dict rather than replacing it.
        self.data = {"id": 7}
        # event code -> the last event's rule details, as the coordinator keeps
        # them for #373.
        self.event_details = {}
        # recorder disks, as the coordinator keeps them for #745
        self.storage_disks = []

    def get_serial_number(self):
        return "SERIAL1"

    def get_device_name(self):
        return "Front Door"

    def get_event_timestamp(self, event_name):
        return self.timestamps.get(event_name, 0)

    def get_event_details(self, event_name):
        return self.event_details.get(event_name, {})

    def get_storage_disks(self):
        return self.storage_disks

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


@pytest.mark.parametrize(
    "event_name,expected",
    [
        ("SmartMotionHuman", "Smart Motion Human"),
        ("SmartMotionVehicle", "Smart Motion Vehicle"),
        ("CrossRegionDetection", "Cross Region Detection"),
        ("AudioMutation", "Audio Mutation"),
        ("AlarmLocal", "Alarm Local"),
        ("StorageNotExist", "Storage Not Exist"),
    ],
)
def test_camel_case_events_become_readable_keys(sensor, event_name, expected):
    """The name itself is in translations/en.json now, and
    test_event_sensor_names_are_translated.py compares the whole file against
    the derivation. What this still pins is that the key an entity declares is
    the slug of the name it used to return, because that slug is also its unique
    id suffix: if it drifted, every one of these sensors would be renamed."""
    assert sensor(event_name).translation_key == expected.lower().replace(" ", "_")


@pytest.mark.parametrize(
    "event_name,expected",
    [
        ("VideoMotion", "Motion Alarm"),
        ("CrossLineDetection", "Cross Line Alarm"),
        ("DoorbellPressed", "Button Pressed"),
    ],
)
def test_overridden_names_win_over_the_derived_one(sensor, event_name, expected):
    """Same three overrides, reached through the key they produce."""
    assert sensor(event_name).translation_key == expected.lower().replace(" ", "_")


def test_a_code_with_no_string_keeps_the_derived_english_name(sensor):
    """IVS must not become I V S, and it is also the fallback in action.

    `get_event_list` reads the config entry, so a hand edited .storage can name
    a code this ships no string for. IVS is exactly that: not in ALL_EVENTS and
    not one of the four a doorbell adds, so it keeps the derived name rather
    than ending up with no name of its own. Which is what makes this the test
    that the fallback is real and not just written down."""
    built = sensor("IVS")

    assert built.name == "IVS"
    assert built.translation_key is None


# --- device classes and icons ----------------------------------------------


@pytest.mark.parametrize(
    "event_name,expected",
    [
        ("VideoMotion", MOTION_SENSOR_DEVICE_CLASS),
        ("AlarmLocal", SAFETY_DEVICE_CLASS),
        ("VideoLoss", SAFETY_DEVICE_CLASS),
        ("VideoBlind", TAMPER_DEVICE_CLASS),
        ("DoorStatus", DOOR_DEVICE_CLASS),
        ("AudioMutation", SOUND_DEVICE_CLASS),
        ("SmartMotionHuman", MOTION_SENSOR_DEVICE_CLASS),  # the fallback
    ],
)
def test_device_class_mapping(sensor, event_name, expected):
    assert sensor(event_name).device_class == expected


def test_no_event_sensor_chooses_an_icon_in_code(sensor):
    """The audio events' volume glyph moved to icons.json, keyed on the same
    slug as their name and their unique id.

    Asserted as None rather than deleted, because `Entity.icon` returns
    `_attr_icon` whenever it is set and would then win over the file silently.
    The glyph itself is pinned in test_entity_icons_come_from_icons_json.py."""
    assert sensor("AudioMutation").icon is None
    assert sensor("VideoMotion").icon is None


# --- identity, including a back-compat case that must not be tidied away ---


def test_video_motion_keeps_the_bare_serial_as_its_id(sensor):
    """Changing this orphans every existing motion sensor on upgrade."""
    assert sensor("VideoMotion").unique_id == "SERIAL1"


def test_other_events_get_a_suffixed_id(sensor):
    assert sensor("SmartMotionHuman").unique_id == "SERIAL1_smart_motion_human"
    assert sensor("CrossLineDetection").unique_id == "SERIAL1_cross_line_alarm"


def test_ids_are_distinct_across_the_events_a_camera_reports(sensor):
    events = [
        "VideoMotion",
        "CrossLineDetection",
        "AlarmLocal",
        "VideoLoss",
        "VideoBlind",
        "AudioMutation",
        "CrossRegionDetection",
        "SmartMotionHuman",
        "SmartMotionVehicle",
    ]
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

    assert (
        c._dahua_event_listeners == {}
    ), "the key outlived the entity, so the poll still thinks something reads it"


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


# --- the event sensor's own timer --------------------------------------------
#
# test_momentary_sensors.py covers what `is_on` and `_hold` decide. What decides
# whether anybody ever asks them again is `_async_event_fired`, and that had nothing
# on it: `is_on` going false on its own is not enough, because nothing would look
# again and the sensor would keep showing on until some unrelated event wrote to it.


@pytest.fixture
async def event_sensor(hass, monkeypatch):
    """A real event sensor with hass attached and its state writes counted."""
    writes = []
    monkeypatch.setattr(bs.DahuaBaseEntity, "__init__", lambda self, c, e: None)
    monkeypatch.setattr(bs.BinarySensorEntity, "__init__", lambda self: None)
    monkeypatch.setattr(
        DahuaEventSensor,
        "schedule_update_ha_state",
        lambda self, force_refresh=False: writes.append(1),
        raising=False,
    )

    def build(event_name, coordinator):
        s = DahuaEventSensor(coordinator, object(), event_name)
        s.hass = hass
        return s

    build.writes = writes
    return build


async def test_a_momentary_event_arms_a_timer_to_look_again(event_sensor):
    """A doorbell press has no Stop coming, so the sensor has to be asked again once
    the hold is up. The timer is the only thing that makes that happen."""
    c = _Coordinator()
    s = event_sensor("DoorbellPressed", c)
    await s.async_added_to_hass()
    c.timestamps["DoorbellPressed"] = int(time.time())

    s._async_event_fired()

    assert event_sensor.writes, "Home Assistant was not told the state changed"
    assert s._unsub_timer is not None, "nothing will ever ask this sensor again"

    await s.async_will_remove_from_hass()


async def test_an_ordinary_event_arms_no_timer(event_sensor):
    """Motion sends a Start and a Stop. Clearing it on a timer would end motion
    detection early for everybody, which is far worse than the bug the hold fixes."""
    c = _Coordinator()
    s = event_sensor("VideoMotion", c)
    await s.async_added_to_hass()
    c.timestamps["VideoMotion"] = int(time.time())

    s._async_event_fired()

    assert event_sensor.writes, "the state was not written"
    assert s._unsub_timer is None, "gave motion a hold it must not have"


async def test_a_code_the_device_pulses_gets_the_default_hold(event_sensor):
    """Whether a code is momentary is learned from the first event rather than known
    at setup, so a code not on the explicit list still gets a timer once the device
    has pulsed it."""
    c = _Coordinator()
    c.event_is_momentary = lambda name: True
    s = event_sensor("CrossLineDetection", c)
    await s.async_added_to_hass()
    c.timestamps["CrossLineDetection"] = int(time.time())

    s._async_event_fired()

    assert s._hold() == DEFAULT_PULSE_HOLD_SECONDS
    assert s._unsub_timer is not None

    await s.async_will_remove_from_hass()


async def test_a_stop_does_not_arm_a_hold(event_sensor):
    """A Stop clears the timestamp. Arming a hold on the way down would schedule a
    wake-up for a sensor that is already off, and on a device that sends Start/Stop
    pairs that is one stray timer per event."""
    c = _Coordinator()
    s = event_sensor("DoorbellPressed", c)
    await s.async_added_to_hass()
    c.timestamps["DoorbellPressed"] = 0

    s._async_event_fired()

    assert event_sensor.writes, "the Stop was not shown"
    assert s._unsub_timer is None, "armed a timer for an event that had ended"


async def test_a_second_press_replaces_the_timer_rather_than_adding_one(event_sensor):
    """Otherwise the first timer still fires and clears the sensor while the second
    press should still be holding it on."""
    c = _Coordinator()
    s = event_sensor("DoorbellPressed", c)
    await s.async_added_to_hass()
    c.timestamps["DoorbellPressed"] = int(time.time())
    s._async_event_fired()
    first = s._unsub_timer

    c.timestamps["DoorbellPressed"] = int(time.time())
    s._async_event_fired()

    assert s._unsub_timer is not first, "re-used the spent timer handle"

    await s.async_will_remove_from_hass()


async def test_the_expired_hold_clears_its_handle_and_asks_again(event_sensor):
    c = _Coordinator()
    s = event_sensor("DoorbellPressed", c)
    await s.async_added_to_hass()
    c.timestamps["DoorbellPressed"] = int(time.time())
    s._async_event_fired()
    # Held deliberately: _async_hold_expired drops the handle without calling it,
    # which is right for a timer that has fired but leaves a real one scheduled here.
    cancel = s._unsub_timer
    before = len(event_sensor.writes)

    s._async_hold_expired()

    assert s._unsub_timer is None
    assert len(event_sensor.writes) > before, "the hold expired without telling anyone"
    cancel()


async def test_removal_cancels_a_pending_hold(event_sensor):
    """A timer left running fires into an entity Home Assistant has already removed."""
    c = _Coordinator()
    s = event_sensor("DoorbellPressed", c)
    await s.async_added_to_hass()
    c.timestamps["DoorbellPressed"] = int(time.time())
    s._async_event_fired()
    assert s._unsub_timer is not None

    await s.async_will_remove_from_hass()

    assert s._unsub_timer is None


async def test_removal_is_safe_when_no_hold_is_pending(event_sensor):
    c = _Coordinator()
    s = event_sensor("VideoMotion", c)
    await s.async_added_to_hass()

    await s.async_will_remove_from_hass()

    assert s._unsub_timer is None


async def test_the_explicit_list_is_used_without_asking_the_device(event_sensor):
    """Both rules apply to a doorbell press once the device has pulsed it, and today
    they happen to give the same five seconds -- so comparing the numbers would prove
    nothing. What the precedence actually means is that the explicit entry is taken
    without consulting `event_is_momentary` at all, which is what this checks. If the
    two values ever diverge, this is already testing the right thing."""
    asked = []
    c = _Coordinator()
    c.event_is_momentary = lambda name: asked.append(name) or True
    s = event_sensor("DoorbellPressed", c)

    hold = s._hold()

    assert hold == MOMENTARY_EVENT_HOLD_SECONDS["DoorbellPressed"]
    assert asked == [], "consulted the device for a code that is on the explicit list"


async def test_a_code_on_neither_list_waits_for_a_stop(event_sensor):
    """The default. An event the device sends Start/Stop for has no hold at all, and
    `None` is what says so -- not a zero, which would read as an expired hold."""
    c = _Coordinator()
    s = event_sensor("VideoMotion", c)

    assert s._hold() is None


# --- the authorized vehicle sensor when it is actually running ---------------
#
# Everything above reads properties off a constructed object. `async_added_to_hass`
# is the largest untested block in this module: it subscribes to plate updates and,
# separately, restores the sensor's state after a reload. A recorder is reloaded
# whenever any of its options change, so that restore path runs often.


@pytest.fixture
async def vehicle(hass, monkeypatch):
    """A real authorized-vehicle sensor, attached to a real hass.

    Async because `hass` is: the only other fixture in this suite that depends on it
    is async too, and that is the shape known to work here.

    Only `schedule_update_ha_state` is stubbed -- the entity is not registered with a
    platform, so the real one has nothing to write to. Calls are counted on the
    fixture itself, because "did it tell Home Assistant" is one of the things worth
    asserting, and hanging an attribute off an HA entity is asking for trouble.
    """
    writes = []
    monkeypatch.setattr(
        DahuaAuthorizedVehicleBinarySensor,
        "schedule_update_ha_state",
        lambda self, force_refresh=False: writes.append(1),
        raising=False,
    )

    def build(coordinator):
        s = DahuaAuthorizedVehicleBinarySensor(coordinator, object())
        s.hass = hass
        return s

    build.writes = writes
    return build


async def test_an_authorized_plate_turns_the_sensor_on_and_arms_the_auto_off(vehicle):
    c = _Coordinator()
    s = vehicle(c)
    await s.async_added_to_hass()
    assert c._plate_listeners, "never subscribed, so the rest proves nothing"

    c._last_plate = "ABC1234"
    c._last_plate_data = {"vehicle_brand": "Volkswagen", "direction": "Approach"}
    c._last_plate_timestamp = int(time.time())
    c._plate_listeners[0]()

    assert s.is_on is True
    assert s._last_matched_plate == "ABC1234"
    assert s._last_matched_plate_data["vehicle_brand"] == "Volkswagen"
    assert s._unsub_timer is not None, "nothing will ever turn this off"
    assert vehicle.writes, "Home Assistant was not told the state changed"

    await s.async_will_remove_from_hass()


async def test_the_matched_plate_data_is_copied_not_referenced(vehicle):
    """The sensor keeps what matched so the attributes still describe that vehicle
    after the coordinator has moved on to the next plate. Holding the coordinator's
    own dict would make the attributes change under the user."""
    c = _Coordinator()
    s = vehicle(c)
    await s.async_added_to_hass()
    c._last_plate = "ABC1234"
    c._last_plate_data = {"vehicle_brand": "Volkswagen"}
    c._last_plate_timestamp = int(time.time())
    c._plate_listeners[0]()

    c._last_plate_data["vehicle_brand"] = "Something else entirely"

    assert s._last_matched_plate_data["vehicle_brand"] == "Volkswagen"
    assert s.extra_state_attributes["last_matched_brand"] == "Volkswagen"

    await s.async_will_remove_from_hass()


async def test_an_unauthorized_plate_does_not_turn_it_on_but_still_refreshes(vehicle):
    """A plate that is not on the list must not raise the sensor. It does still write
    state, because the attributes carry the authorized list and the hold time and a
    reader looking at the card should see current values."""
    c = _Coordinator()
    s = vehicle(c)
    await s.async_added_to_hass()

    c._last_plate = "UNKNOWN99"
    c._last_plate_timestamp = int(time.time())
    c._plate_listeners[0]()

    assert s.is_on is False
    assert s._unsub_timer is None, "armed a timer for a plate it did not match"
    assert s._last_matched_plate is None
    assert vehicle.writes, "an unauthorized plate left the card stale"


async def test_a_second_authorized_plate_does_not_leave_two_timers(vehicle):
    """The second match cancels the first timer before arming its own. Without that
    the earlier one still fires and clears the sensor while the later plate should
    still be holding it on."""
    c = _Coordinator()
    s = vehicle(c)
    await s.async_added_to_hass()
    c._last_plate = "ABC1234"
    c._last_plate_timestamp = int(time.time())
    c._plate_listeners[0]()
    first = s._unsub_timer

    c._last_plate = "XYZ5678"
    c._last_plate_timestamp = int(time.time())
    c._plate_listeners[0]()

    assert s._unsub_timer is not first, "re-used the old timer handle"
    assert s._last_matched_plate == "XYZ5678"

    await s.async_will_remove_from_hass()


# --- restoring state across a reload ----------------------------------------


async def test_a_plate_seen_just_before_a_reload_is_still_on_afterwards(vehicle):
    """The reason the recheck exists. A recorder reloads whenever an option changes,
    and a vehicle recognised seconds earlier should not be forgotten because of it."""
    c = _Coordinator()
    c._last_plate = "ABC1234"
    c._last_plate_data = {"vehicle_brand": "Volkswagen"}
    c._last_plate_timestamp = int(time.time()) - 5
    s = vehicle(c)

    await s.async_added_to_hass()

    assert s.is_on is True
    assert s._last_matched_plate == "ABC1234"
    assert s._unsub_timer is not None

    await s.async_will_remove_from_hass()


async def test_the_restored_hold_runs_from_when_the_plate_was_seen(vehicle):
    """Not from when the reload happened. Measuring the hold from now would extend it
    by however long the sensor was off, so every reload would keep the sensor on for
    a further full hold -- and on a recorder whose options are being adjusted, that
    stacks up."""
    c = _Coordinator()
    seen_at = int(time.time()) - 50  # 50s ago, hold is 60s
    c._last_plate = "ABC1234"
    c._last_plate_timestamp = seen_at
    s = vehicle(c)

    await s.async_added_to_hass()

    # 10s of hold left, not 60. Compared against the plate's own timestamp rather
    # than against now, which is the whole point.
    assert s._active_until == seen_at + 60
    assert s._active_until - time.time() < 15, "the hold was restarted, not resumed"

    await s.async_will_remove_from_hass()


async def test_a_plate_older_than_the_hold_does_not_come_back_on(vehicle):
    c = _Coordinator()
    c._last_plate = "ABC1234"
    c._last_plate_timestamp = int(time.time()) - 120  # hold is 60
    s = vehicle(c)

    await s.async_added_to_hass()

    assert s.is_on is False
    assert s._unsub_timer is None
    assert s._last_matched_plate is None


async def test_an_unauthorized_recent_plate_does_not_come_back_on(vehicle):
    c = _Coordinator()
    c._last_plate = "UNKNOWN99"
    c._last_plate_timestamp = int(time.time())
    s = vehicle(c)

    await s.async_added_to_hass()

    assert s.is_on is False
    assert s._unsub_timer is None


async def test_a_fresh_install_with_no_plate_yet_restores_nothing(vehicle):
    """The timestamp guard. Without it a zero timestamp is compared against the hold
    and, on a coordinator that has never seen a plate, `is_plate_authorized` decides
    the outcome of an event that never happened."""
    c = _Coordinator()
    assert c._last_plate_timestamp == 0
    s = vehicle(c)

    await s.async_added_to_hass()

    assert s.is_on is False
    assert s._unsub_timer is None


# --- letting go -------------------------------------------------------------


async def test_the_auto_off_clears_its_own_handle(vehicle):
    """`_unsub_timer` is what the next match cancels and what removal cancels. A fired
    timer that leaves its handle behind would have both calling a spent callback."""
    c = _Coordinator()
    s = vehicle(c)
    await s.async_added_to_hass()
    c._last_plate = "ABC1234"
    c._last_plate_timestamp = int(time.time())
    c._plate_listeners[0]()
    assert s._unsub_timer is not None
    # Held on to deliberately: _async_auto_off drops the handle without calling it,
    # which is right when the timer has just fired but leaves a real one scheduled
    # here. Cancelling it keeps the test from tripping the lingering-timer check.
    cancel = s._unsub_timer

    s._async_auto_off()

    assert s._unsub_timer is None
    assert vehicle.writes
    cancel()


async def test_removal_stops_the_plate_subscription(vehicle):
    """Same fault this sensor had before #842: the callback outlived the entity."""
    c = _Coordinator()
    s = vehicle(c)
    await s.async_added_to_hass()
    assert c._plate_listeners

    assert s._on_remove, "registered nothing to undo the subscription"
    for undo in list(s._on_remove):
        undo()

    assert c._plate_listeners == [], "the callback outlived the entity"


async def test_removal_cancels_a_pending_auto_off(vehicle):
    """A timer left running after removal fires into an entity Home Assistant has
    already taken away."""
    c = _Coordinator()
    s = vehicle(c)
    await s.async_added_to_hass()
    c._last_plate = "ABC1234"
    c._last_plate_timestamp = int(time.time())
    c._plate_listeners[0]()
    assert s._unsub_timer is not None

    await s.async_will_remove_from_hass()

    assert s._unsub_timer is None


async def test_removal_is_safe_with_no_timer_pending(vehicle):
    c = _Coordinator()
    s = vehicle(c)
    await s.async_added_to_hass()

    await s.async_will_remove_from_hass()

    assert s._unsub_timer is None


# --- #373: an IVS/smart sensor exposes which rule tripped --------------------


def test_an_ivs_sensor_exposes_the_rule_details_with_the_base_attrs():
    """Built directly so the real base __init__ runs, the way the authorized
    vehicle attribute test does, because the rule details are layered on the
    base's id and integration rather than replacing them."""
    c = _Coordinator()
    c.event_details["CrossLineDetection"] = {
        "rule_name": "Pool Entry",
        "direction": "LeftToRight",
        "object_type": "Human",
    }
    s = DahuaEventSensor(c, object(), "CrossLineDetection")

    attrs = s.extra_state_attributes
    assert attrs["rule_name"] == "Pool Entry"
    assert attrs["direction"] == "LeftToRight"
    assert attrs["object_type"] == "Human"
    assert attrs["id"] == "7"
    assert attrs["integration"] == "dahua"


def test_a_sensor_without_details_keeps_only_the_base_attrs():
    c = _Coordinator()
    s = DahuaEventSensor(c, object(), "VideoMotion")

    attrs = s.extra_state_attributes
    assert "rule_name" not in attrs
    assert "object_type" not in attrs
    assert attrs["id"] == "7"


# --- #745: a recorder's disk-health sensor ----------------------------------


def _disk(name="/dev/sda", healthy=True, has_error=False, state="Success"):
    return {
        "name": name,
        "state": state,
        "healthy": healthy,
        "total_bytes": 6_000_000_000,
        "used_bytes": 5_000_000_000,
        "has_error": has_error,
        "health_flag": 0,
    }


def test_a_disk_problem_sensor_is_off_when_the_disk_is_healthy():
    c = _Coordinator()
    c.storage_disks = [_disk()]
    s = DahuaDiskProblemBinarySensor(c, object(), "/dev/sda")

    assert s.is_on is False
    assert s.unique_id == "SERIAL1_disk_dev_sda"
    attrs = s.extra_state_attributes
    assert attrs["state"] == "Success"
    assert attrs["total_gb"] == 6.0
    assert attrs["partition_error"] is False
    assert attrs["id"] == "7"  # base id preserved, not replaced


def test_a_disk_problem_sensor_is_on_when_the_disk_is_unhealthy():
    c = _Coordinator()
    c.storage_disks = [_disk(healthy=False, has_error=True)]
    s = DahuaDiskProblemBinarySensor(c, object(), "/dev/sda")

    assert s.is_on is True
    assert s.extra_state_attributes["partition_error"] is True


def test_a_disk_no_longer_reported_reads_unknown():
    c = _Coordinator()
    c.storage_disks = []  # the disk has gone
    s = DahuaDiskProblemBinarySensor(c, object(), "/dev/sda")

    assert s.is_on is None
    # the base attributes still resolve even with no disk
    assert s.extra_state_attributes["id"] == "7"


def test_a_disk_problem_sensor_is_diagnostic_and_off_by_default():
    c = _Coordinator()
    c.storage_disks = [_disk()]
    s = DahuaDiskProblemBinarySensor(c, object(), "/dev/sda")

    assert s.entity_registry_enabled_default is False
    assert s.entity_category is not None
