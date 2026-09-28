"""A doorbell press is a moment, not a state, so its sensor has to clear itself.

#486 and #375. Most events arrive as a pair: a Start raises the sensor and a
Stop clears it. A doorbell press is a single notification that something
happened. Nothing follows it, so nothing ever turned the sensor off:

    return self._coordinator.get_event_timestamp(self._event_name) > 0

Restarting Home Assistant cleared it, because the state was rebuilt, not
because anything resolved. #486 is an IMOU DB61i whose button sensor stayed on
until a restart; #375 is a VTO whose `call_no_answered` did the same.

The mechanism already existed in this file. The authorized vehicle sensor holds
itself on for a configured time and then clears itself with `async_call_later`.
This applies the same idea to the events that need it.

**Only to the events that need it.** `VideoMotion` and the IVS codes genuinely
use Start and Stop, and clearing those on a timer would end motion detection
early for everybody, which is far worse than the bug being fixed. So it is a
short table rather than a rule, and anything not in it behaves exactly as before.

Whichever comes first wins: a device that does send a closing event still clears
the sensor immediately, so this only adds a floor for devices that never send one.
"""
import time
from types import SimpleNamespace

from custom_components.dahua.binary_sensor import (
    MOMENTARY_EVENT_HOLD_SECONDS,
    DahuaEventSensor,
)


def _sensor(event_name, started_ago=None, momentary=False):
    """A sensor for `event_name`, with its event fired `started_ago` secs back.

    `momentary` is what the coordinator reports when a code has arrived as a
    Pulse -- there is no Stop coming for one, so the sensor has to clear itself
    even though the code is not in the explicit list.
    """
    sensor = object.__new__(DahuaEventSensor)
    sensor._event_name = event_name
    sensor._hold_seconds = MOMENTARY_EVENT_HOLD_SECONDS.get(event_name)
    sensor._unsub_timer = None
    stamp = 0 if started_ago is None else int(time.time()) - started_ago
    sensor._coordinator = SimpleNamespace(
        get_event_timestamp=lambda name: stamp,
        event_is_momentary=lambda name: momentary)
    return sensor


# --- the bug -----------------------------------------------------------------

def test_a_press_turns_the_sensor_on():
    assert _sensor("DoorbellPressed", started_ago=0).is_on is True


def test_a_press_does_not_stay_on_for_ever():
    """#486: it stayed on until Home Assistant was restarted."""
    hold = MOMENTARY_EVENT_HOLD_SECONDS["DoorbellPressed"]

    assert _sensor("DoorbellPressed", started_ago=hold + 1).is_on is False


def test_an_unanswered_call_clears_itself_too():
    """#375, the same fault on a different event."""
    hold = MOMENTARY_EVENT_HOLD_SECONDS["CallNoAnswered"]

    assert _sensor("CallNoAnswered", started_ago=0).is_on is True
    assert _sensor("CallNoAnswered", started_ago=hold + 1).is_on is False


def test_the_sensor_is_still_on_inside_the_hold():
    """It must last long enough for an automation to see it."""
    assert _sensor("DoorbellPressed", started_ago=1).is_on is True


# --- what must not change ----------------------------------------------------

def test_motion_is_not_given_a_hold():
    """The dangerous mistake. Motion uses Start and Stop and must keep doing so.

    A hold here would clear a motion sensor while the motion was still going on,
    which would break far more than it fixed.
    """
    assert "VideoMotion" not in MOMENTARY_EVENT_HOLD_SECONDS

    long_motion = _sensor("VideoMotion", started_ago=3600)
    assert long_motion.is_on is True, "an hour of motion is still motion"


def test_the_ivs_codes_are_not_given_a_hold():
    for code in ("CrossLineDetection", "CrossRegionDetection",
                 "SmartMotionHuman", "SmartMotionVehicle"):
        assert code not in MOMENTARY_EVENT_HOLD_SECONDS, code
        assert _sensor(code, started_ago=3600).is_on is True, code


def test_an_ordinary_event_that_never_fired_is_off():
    assert _sensor("VideoMotion").is_on is False


def test_a_momentary_event_that_never_fired_is_off():
    assert _sensor("DoorbellPressed").is_on is False


def test_a_device_that_does_send_a_stop_still_clears_immediately():
    """The hold is a floor, not a delay.

    A VTO reports a non-ringing state when the call ends, which zeroes the
    timestamp. That must still clear the sensor at once rather than waiting.
    """
    sensor = _sensor("DoorbellPressed", started_ago=0)
    assert sensor.is_on is True

    sensor._coordinator = SimpleNamespace(get_event_timestamp=lambda name: 0)
    assert sensor.is_on is False


# --- the timer ---------------------------------------------------------------

def test_the_hold_is_long_enough_to_be_useful():
    """Short enough to feel momentary, long enough for an automation to fire."""
    for event, hold in MOMENTARY_EVENT_HOLD_SECONDS.items():
        assert 2 <= hold <= 60, "%s holds for %s seconds" % (event, hold)


async def test_removing_the_entity_drops_its_timer():
    """A timer that outlives its entity fires into nothing."""
    cancelled = []
    sensor = _sensor("DoorbellPressed", started_ago=0)
    sensor._unsub_timer = lambda: cancelled.append(True)

    await sensor.async_will_remove_from_hass()

    assert cancelled == [True]
    assert sensor._unsub_timer is None


# --- a Pulse that is not a doorbell call state (#573) ------------------------
#
# The Pulse branch of _dispatch_event handled DoorStatus and treated everything
# else as a doorbell call state, reading `Data.State` out of it. A code that
# carries no State read 0, 0 is not in DOORBELL_RINGING_STATES, and the sensor
# was written *off*. Thirteen of the forty-two selectable codes are Pulse
# shaped, so thirteen sensors could never turn on -- FaceDetection,
# FaceRecognition, HumanTrait, InterVideoAccess, NewFile, NTPAdjustTime,
# TimeChange, IntelliFrame, AlarmOutput, MDResult and the three Traffic codes.
#
# Which codes those are is deliberately NOT a list in the integration. Only
# InterVideoAccess has ever been observed as a Pulse in a report (#329), and the
# selectable set gained two codes in a week. The coordinator records what the
# device actually sent, and the sensor asks it.

def test_a_pulse_code_clears_itself_even_though_it_is_not_in_the_list():
    """It has no entry in MOMENTARY_EVENT_HOLD_SECONDS, so without the
    coordinator's answer it would stay on until Home Assistant restarted."""
    sensor = _sensor("FaceDetection", started_ago=99, momentary=True)

    assert sensor.is_on is False


def test_a_pulse_code_is_on_while_the_hold_lasts():
    sensor = _sensor("FaceDetection", started_ago=1, momentary=True)

    assert sensor.is_on is True


def test_the_same_code_stays_on_when_the_device_never_pulsed_it():
    """A Start/Stop code must keep waiting for its Stop. Clearing those on a
    timer would end motion detection early for everybody."""
    sensor = _sensor("FaceDetection", started_ago=99, momentary=False)

    assert sensor.is_on is True


def test_the_explicit_list_still_wins():
    """DoorbellPressed is 5s by name, and must not be changed by this."""
    assert _sensor("DoorbellPressed", started_ago=99, momentary=True).is_on is False
    assert _sensor("DoorbellPressed", started_ago=1, momentary=True).is_on is True


def test_a_never_fired_pulse_code_is_off():
    assert _sensor("FaceDetection", momentary=True).is_on is False


def test_the_hold_resolves_per_read_not_at_construction():
    """Whether a code is momentary is learned from the first event, so a sensor
    built before that event has to pick it up afterwards."""
    sensor = _sensor("FaceDetection", started_ago=99, momentary=False)
    assert sensor.is_on is True

    sensor._coordinator.event_is_momentary = lambda name: True

    assert sensor.is_on is False, "the answer is read each time, not cached"
