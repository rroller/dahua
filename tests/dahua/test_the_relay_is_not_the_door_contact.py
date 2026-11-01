"""The relay is not the door contact.

#1166: a DHI-VTO2202F-P-S2 opening a gate reports two different things down the
one `DoorStatus` code, within milliseconds of each other:

    DoorStatus {"Relay":true,"Status":"Close"}
    DoorStatus {"DoorIndex":0,"Status":"Close"}
    DoorStatus {"DoorIndex":0,"Status":"Open"}
    DoorStatus {"Relay":true,"Status":"Close"}
    DoorStatus {"DoorIndex":0,"Status":"Close"}

One Door Status sensor exists and it is the physical contact's, so the relay's
`Close` wrote the gate shut while it was still swinging. The reporter's own
workaround was to filter the relay events out.

Filtered outright it would be a different bug: a VTO with nothing wired to its
door-sensor input may have only the relay form to report, and a sensor that can
never move is indistinguishable from a device that has stopped sending. So the
relay drives the sensor until a contact event proves there is something better,
and never again after that.

Either way the relay event still reaches the `dahua_event_received` bus and
diagnostics' recent-event buffer: both transports fire the bus event before
_dispatch_event is called, so nothing an automation could read is lost.
"""

from types import SimpleNamespace

import pytest

from custom_components.dahua import (
    DahuaDataUpdateCoordinator,
    door_status_is_relay,
)

# --- which shape is which ------------------------------------------------------


def test_the_reported_relay_payload_is_the_relay():
    assert door_status_is_relay({"Data": {"Relay": True, "Status": "Close"}})


def test_the_reported_contact_payload_is_not():
    assert not door_status_is_relay({"Data": {"DoorIndex": 0, "Status": "Close"}})


def test_the_payload_every_other_vto_sends_is_not():
    """A VTO2000A sends Status and the timestamps, and nothing else. It must
    keep driving its sensor exactly as it always has."""
    assert not door_status_is_relay(
        {"Data": {"Status": "Open", "LocaleTime": "2026-10-31 19:49:46", "UTC": 1}}
    )


def test_the_key_decides_and_not_the_value():
    """Both relay lines in the report carry `Relay: true` with the gate in
    opposite states, so that true marks the event as the relay's rather than
    reporting the relay's own position."""
    assert door_status_is_relay({"Data": {"Relay": False, "Status": "Open"}})
    assert door_status_is_relay({"Data": {"Relay": "whatever", "Status": "Open"}})


def test_a_payload_naming_a_door_is_left_alone():
    """Narrowed to the shape that was measured. A device that says which door
    an event is about is saying something about a door, and no payload
    carrying both has been seen."""
    assert not door_status_is_relay(
        {"Data": {"Relay": True, "DoorIndex": 0, "Status": "Close"}}
    )


def test_the_cgi_spelling_of_the_payload_is_read():
    """DHIP sends `Data`; the CGI wire format parses to a lowercase `data`.
    Reading only the first would leave the fault in place on that transport."""
    assert door_status_is_relay({"data": {"Relay": True, "Status": "Close"}})
    assert not door_status_is_relay({"data": {"DoorIndex": 0, "Status": "Close"}})


def test_nothing_unreadable_raises():
    """This runs inside the event handler: an exception here takes down the
    stream for every event after it (#475)."""
    assert not door_status_is_relay({})
    assert not door_status_is_relay({"Data": "truncated"})
    assert not door_status_is_relay({"Data": None})


# --- what it does to the sensor, which is the part that was wrong --------------


def _vto(channel=0):
    """A coordinator with just enough of itself to run the VTO event handler."""
    c = object.__new__(DahuaDataUpdateCoordinator)
    c._channel = channel
    c._dahua_event_timestamp = {}
    c._dahua_event_listeners = {}
    c.hass = SimpleNamespace(bus=SimpleNamespace(fire=lambda *a, **k: None))
    c.get_device_name = lambda: "Front Gate"
    c._handle_anpr_plate = lambda event: None
    return c


def _listening(coordinator):
    key = coordinator.get_event_key("DoorStatus")
    coordinator.add_dahua_event_listener("DoorStatus", lambda: None)
    return key


def _event(payload, key="Data"):
    return {"Code": "DoorStatus", "Action": "Pulse", key: payload}


def _relay(status, key="Data"):
    return _event({"Relay": True, "Status": status}, key)


def _contact(status, key="Data"):
    return _event({"DoorIndex": 0, "Status": status}, key)


@pytest.mark.parametrize("payload_key", ["Data", "data"])
def test_the_reported_sequence_leaves_the_gate_open(payload_key):
    """The five lines from the report, in their order. The gate is open from
    the third line on, and the relay's Close must not close it."""
    c = _vto()
    key = _listening(c)

    c.on_receive_vto_event(_relay("Close", payload_key))
    c.on_receive_vto_event(_contact("Close", payload_key))
    c.on_receive_vto_event(_contact("Open", payload_key))
    open_at = c._dahua_event_timestamp[key]
    assert open_at > 0, "the contact said Open"

    c.on_receive_vto_event(_relay("Close", payload_key))

    assert (
        c._dahua_event_timestamp[key] == open_at
    ), "the relay closing reported the gate shut while it was still swinging"

    # And the contact still closes it, which is the whole point of keeping it.
    c.on_receive_vto_event(_contact("Close", payload_key))
    assert c._dahua_event_timestamp[key] == 0


def test_a_device_that_only_sends_the_relay_form_still_drives_its_sensor():
    """The risk of filtering outright: nothing wired to the door-sensor input,
    so the relay is all there is to report."""
    c = _vto()
    key = _listening(c)

    c.on_receive_vto_event(_relay("Open"))
    assert c._dahua_event_timestamp[key] > 0

    c.on_receive_vto_event(_relay("Close"))
    assert c._dahua_event_timestamp[key] == 0


def test_a_relay_event_is_never_taken_for_a_contact():
    c = _vto()
    _listening(c)

    c.on_receive_vto_event(_relay("Open"))

    assert c._door_contact_seen is False


def test_a_contact_event_is_what_turns_the_filter_on():
    c = _vto()
    _listening(c)

    c.on_receive_vto_event(_contact("Close"))

    assert c._door_contact_seen is True


def test_a_vto_that_names_no_door_at_all_turns_it_on_too():
    """The VTO2000A payload. It is a contact event, so it must take precedence
    over the relay on a device that sends both shapes."""
    c = _vto()
    key = _listening(c)

    c.on_receive_vto_event(_event({"Status": "Open"}))
    open_at = c._dahua_event_timestamp[key]

    c.on_receive_vto_event(_relay("Close"))

    assert c._dahua_event_timestamp[key] == open_at


def test_a_payload_carrying_both_still_writes_the_sensor():
    """Unmeasured shape, unchanged behaviour: it is read as a contact event."""
    c = _vto()
    key = _listening(c)

    c.on_receive_vto_event(_event({"Relay": True, "DoorIndex": 0, "Status": "Open"}))

    assert c._dahua_event_timestamp[key] > 0
    assert c._door_contact_seen is True


def test_the_second_doors_relay_is_still_not_the_first_doors_business():
    """The #488 guard comes first: an Index that is not door 1's is dropped
    before any of this is asked."""
    c = _vto()
    key = _listening(c)

    c.on_receive_vto_event(_contact("Open"))
    open_at = c._dahua_event_timestamp[key]

    second = _relay("Close")
    second["Index"] = 1
    c.on_receive_vto_event(second)

    assert c._dahua_event_timestamp[key] == open_at
    assert c._door_contact_seen is True


def test_the_filter_is_per_coordinator():
    """Two doorbells on one Home Assistant: what one has learned about its own
    events says nothing about the other's."""
    learned = _vto()
    _listening(learned)
    learned.on_receive_vto_event(_contact("Close"))

    fresh = _vto()
    key = _listening(fresh)
    fresh.on_receive_vto_event(_relay("Open"))

    assert fresh._dahua_event_timestamp[key] > 0
