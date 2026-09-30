"""A Pulse event that is not a doorbell call state should reach its sensor.

`_dispatch_event`'s Pulse branch handled `DoorStatus`, and treated **everything
else** as a doorbell call state:

    state = event.get("Data", {}).get("State", 0)
    pressed = numeric_state in DOORBELL_RINGING_STATES     # {1, 2}
    if pressed: ...raise...
    else: ...write it off...

A code carrying no `State` reads 0, 0 is not a ringing state, and the sensor was
written **off**. Thirteen of the forty-two selectable codes are Pulse shaped, so
thirteen sensors could never turn on: FaceDetection, FaceRecognition, HumanTrait,
InterVideoAccess, NewFile, NTPAdjustTime, TimeChange, IntelliFrame, AlarmOutput,
MDResult, Traffic, TrafficJunction and TrafficSnapshot. That is the defect under
#573, and part of #336 and #456. `FaceRecognition` and `HumanTrait` were added to
the selectable set days ago (#768, #769), so it was still growing.

**Which codes are Pulse shaped is not a list in the integration, deliberately.**
It would be a guess: only `InterVideoAccess` has ever been observed as a Pulse in
a report (#329), the rest is inference from vendor documentation, and the
selectable set changes. The device says `action=Pulse`; the coordinator records
that and the sensor asks it.
"""
import time
from types import SimpleNamespace

from custom_components.dahua import DahuaDataUpdateCoordinator


def _coordinator():
    c = object.__new__(DahuaDataUpdateCoordinator)
    c._channel = 0
    c._dahua_event_timestamp = {}
    c._dahua_event_listeners = {}
    c._recent_events = None
    return c


def _listening(coordinator, *codes):
    """Give each code a listener, which is what _dispatch_event requires."""
    fired = []
    for code in codes:
        key = coordinator.get_event_key(code)
        coordinator._dahua_event_listeners[key] = [lambda: fired.append(1)]
    return fired


def _pulse(coordinator, code, data=None):
    event = {"Code": code, "Action": "Pulse"}
    if data is not None:
        event["Data"] = data
    coordinator._dispatch_event(event, "Pulse")


def _stamp(coordinator, code):
    return coordinator._dahua_event_timestamp.get(coordinator.get_event_key(code), 0)


# --- an action nobody recognises -------------------------------------------


def test_an_unrecognised_action_reaches_no_listener():
    """`_dispatch_event` handles Start, Stop and Pulse. Anything else is skipped
    before the listeners are called, which is the right answer: firing a sensor on
    an action whose meaning is unknown would raise it and never lower it, because
    the matching Stop would not be recognised either.
    """
    coordinator = _coordinator()
    fired = _listening(coordinator, "VideoMotion")

    coordinator._dispatch_event({"Code": "VideoMotion", "Action": "Nonsense"},
                                "Nonsense")

    assert fired == [], "a listener was called for an action nobody understands"
    assert _stamp(coordinator, "VideoMotion") == 0


def test_an_unrecognised_action_does_not_clear_a_running_event():
    """It skips rather than writing 0. A Start followed by something unrecognised
    must leave the sensor where it was, or an unknown action becomes a Stop."""
    coordinator = _coordinator()
    _listening(coordinator, "VideoMotion")
    coordinator._dispatch_event({"Code": "VideoMotion", "Action": "Start"}, "Start")
    running = _stamp(coordinator, "VideoMotion")
    assert running > 0, "the fixture must start the event for this to mean anything"

    coordinator._dispatch_event({"Code": "VideoMotion", "Action": "Nonsense"},
                                "Nonsense")

    assert _stamp(coordinator, "VideoMotion") == running


# --- the bug --------------------------------------------------------------

def test_a_pulse_with_no_state_raises_its_sensor():
    """This is the whole issue: it used to write the sensor off instead."""
    c = _coordinator()
    _listening(c, "FaceDetection")

    _pulse(c, "FaceDetection")

    assert _stamp(c, "FaceDetection") > 0


def test_it_is_recorded_as_momentary_so_the_sensor_clears_itself():
    """There is no Stop coming, so without this the sensor would stay on until
    Home Assistant restarted."""
    c = _coordinator()
    _listening(c, "FaceDetection")

    _pulse(c, "FaceDetection")

    assert c.event_is_momentary("FaceDetection") is True


def test_a_code_that_has_not_pulsed_is_not_momentary():
    c = _coordinator()
    _listening(c, "VideoMotion")

    assert c.event_is_momentary("VideoMotion") is False


def test_the_listener_is_called():
    c = _coordinator()
    fired = _listening(c, "InterVideoAccess")

    _pulse(c, "InterVideoAccess", {"Type": "WebAllLogout"})

    assert fired, "the sensor is never asked for its state otherwise"


def test_a_pulse_carrying_unrelated_data_still_raises_it():
    """#329's capture: InterVideoAccess arrives with a Type and no State."""
    c = _coordinator()
    _listening(c, "InterVideoAccess")

    _pulse(c, "InterVideoAccess", {"Type": "WebAllLogout"})

    assert _stamp(c, "InterVideoAccess") > 0


# --- what must not change -------------------------------------------------

def test_a_doorbell_ring_is_still_read_as_a_call_state():
    c = _coordinator()
    _listening(c, "DoorbellPressed")

    _pulse(c, "DoorbellPressed", {"State": 1})

    assert _stamp(c, "DoorbellPressed") > 0
    assert c.event_is_momentary("DoorbellPressed") is False, (
        "the doorbell has its own hold and must not be handled as a bare Pulse")


def test_a_doorbell_state_that_is_not_a_ring_still_clears_it():
    """State 0 ends a call. It must not be read as "something happened"."""
    c = _coordinator()
    _listening(c, "DoorbellPressed")
    _pulse(c, "DoorbellPressed", {"State": 1})

    _pulse(c, "DoorbellPressed", {"State": 0})

    assert _stamp(c, "DoorbellPressed") == 0


def test_an_open_door_still_raises_door_status():
    c = _coordinator()
    _listening(c, "DoorStatus")

    _pulse(c, "DoorStatus", {"Status": "Open"})

    assert _stamp(c, "DoorStatus") > 0
    assert c.event_is_momentary("DoorStatus") is False


def test_a_closed_door_still_clears_door_status():
    c = _coordinator()
    _listening(c, "DoorStatus")
    _pulse(c, "DoorStatus", {"Status": "Open"})

    _pulse(c, "DoorStatus", {"Status": "Close"})

    assert _stamp(c, "DoorStatus") == 0


def test_a_refused_access_control_card_still_leaves_the_sensor_off():
    """AccessControl's Pulse carries a State too: 1 is a granted card, 0 is not.

    Missed on the first attempt, and caught by test_cgi_events_pulse.py. Treating
    it as a bare moment raises the sensor on a *refused* card, which is the worst
    possible direction to get wrong on a door.
    """
    c = _coordinator()
    _listening(c, "AccessControl")

    _pulse(c, "AccessControl", {"State": 0})

    assert _stamp(c, "AccessControl") == 0


def test_a_granted_access_control_card_raises_it():
    c = _coordinator()
    _listening(c, "AccessControl")

    _pulse(c, "AccessControl", {"State": 1})

    assert _stamp(c, "AccessControl") > 0
    assert c.event_is_momentary("AccessControl") is False


def test_start_and_stop_are_untouched():
    """The IVS codes genuinely pair up, and clearing those on a timer would end
    motion detection early for everybody."""
    c = _coordinator()
    _listening(c, "VideoMotion")

    c._dispatch_event({"Code": "VideoMotion", "action": "Start"}, "Start")
    assert _stamp(c, "VideoMotion") > 0
    assert c.event_is_momentary("VideoMotion") is False

    c._dispatch_event({"Code": "VideoMotion", "action": "Stop"}, "Stop")
    assert _stamp(c, "VideoMotion") == 0


# --- and it never becomes the thing that raises ---------------------------

def test_a_coordinator_that_has_never_dispatched_answers_anyway():
    """Most tests build one of these with object.__new__ and set only what they
    are about. A sensor asking this must not be what breaks one."""
    c = object.__new__(DahuaDataUpdateCoordinator)
    c._channel = 0

    assert c.event_is_momentary("FaceDetection") is False
