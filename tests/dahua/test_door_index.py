"""A second door must not answer for the first.

#488: a VTO2111D-P-S2 paired with a DEE1010B-S2 access control extension module
has two door locks. Both can be opened from Home Assistant -- the service takes
a door number -- but there is one Door Status sensor, and the reporter could not
see the second door's state.

It is worse than a missing sensor. The VTO puts the door number in the event's
`Index`:

    {"Code": "DoorStatus", "Action": "Pulse",
     "Data": {"Status": "Close", ...}, "Index": 0}

and the handler built its listener key from the event code and the coordinator's
channel, never the index. So both doors drove the same sensor, and door 2
closing reported door 1 closed while it stood open.

One sensor exists, it belongs to door 1, and only door 1 may write to it. The
second door's events still reach the `dahua_event_received` bus with their Index
intact, which is where an automation can read them until there is an entity.
"""

from types import SimpleNamespace

import pytest

from custom_components.dahua import DahuaDataUpdateCoordinator, door_index


# --- the doors -----------------------------------------------------------------

def test_the_first_door_is_zero():
    assert door_index({"Code": "DoorStatus", "Index": 0}) == 0


def test_the_extension_module_door_is_one():
    assert door_index({"Code": "DoorStatus", "Index": 1}) == 1


def test_a_third_door_keeps_its_number():
    assert door_index({"Index": 2}) == 2


# --- everything that is not a door number --------------------------------------

def test_no_index_at_all_is_the_first_door():
    """A single-door VTO does not always send one."""
    assert door_index({"Code": "DoorStatus"}) == 0
    assert door_index({}) == 0


def test_minus_one_is_not_a_door():
    """The same device puts Index: -1 on BackKeyLight, so a negative means
    'no door number here' rather than a door before the first."""
    assert door_index({"Index": -1}) == 0


def test_a_number_sent_as_text_is_still_a_number():
    assert door_index({"Index": "1"}) == 1
    assert door_index({"Index": " 2 "}) == 2


def test_something_unreadable_is_the_first_door():
    """Never raise: this runs inside the event handler, and an exception there
    would take down the stream for every event that followed."""
    assert door_index({"Index": None}) == 0
    assert door_index({"Index": "front"}) == 0
    assert door_index({"Index": [1]}) == 0
    assert door_index({"Index": {}}) == 0


@pytest.mark.parametrize("value,expected", [
    (0, 0), (1, 1), (7, 7), (-3, 0), ("0", 0), ("4", 4),
])
def test_the_mapping_in_full(value, expected):
    assert door_index({"Index": value}) == expected


# --- what it does to the sensor, which is the part that was wrong ---------------

def _vto(channel=0):
    """A coordinator with just enough of itself to run the VTO event handler."""
    c = object.__new__(DahuaDataUpdateCoordinator)
    c._channel = channel
    c._dahua_event_timestamp = {}
    c._dahua_event_listeners = {}
    c.fired = []
    c.hass = SimpleNamespace(bus=SimpleNamespace(fire=lambda *a, **k: None))
    c.get_device_name = lambda: "Front Door"
    c._handle_anpr_plate = lambda event: None
    return c


def _door_event(status, index):
    return {"Code": "DoorStatus", "Action": "Pulse",
            "Data": {"Status": status, "LocaleTime": "2026-09-19 08:00:00"},
            "Index": index}


def _listening(coordinator):
    """Register a listener for DoorStatus, as binary_sensor.py does."""
    key = coordinator.get_event_key("DoorStatus")
    coordinator._dahua_event_listeners[key] = lambda: coordinator.fired.append(key)
    return key


def test_the_first_door_still_drives_the_sensor():
    c = _vto()
    key = _listening(c)

    c.on_receive_vto_event(_door_event("Open", 0))

    assert c._dahua_event_timestamp[key] > 0
    assert c.fired == [key]


def test_the_first_door_closing_clears_it():
    c = _vto()
    key = _listening(c)

    c.on_receive_vto_event(_door_event("Open", 0))
    c.on_receive_vto_event(_door_event("Close", 0))

    assert c._dahua_event_timestamp[key] == 0


def test_the_second_door_closing_does_not_close_the_first():
    """The reported fault: one sensor, two doors, last event wins."""
    c = _vto()
    key = _listening(c)

    c.on_receive_vto_event(_door_event("Open", 0))
    open_at = c._dahua_event_timestamp[key]

    c.on_receive_vto_event(_door_event("Close", 1))

    assert c._dahua_event_timestamp[key] == open_at, (
        "door 2 closing reported door 1 closed while it stood open")


def test_the_second_door_opening_does_not_open_the_first():
    c = _vto()
    key = _listening(c)

    c.on_receive_vto_event(_door_event("Open", 1))

    assert c._dahua_event_timestamp.get(key, 0) == 0
    assert c.fired == [], "nothing should have been told the first door moved"


def test_a_vto_that_sends_no_index_is_unchanged():
    """A single-door VTO must keep working exactly as it did."""
    c = _vto()
    key = _listening(c)

    c.on_receive_vto_event({"Code": "DoorStatus", "Action": "Pulse",
                            "Data": {"Status": "Open"}})

    assert c._dahua_event_timestamp[key] > 0
