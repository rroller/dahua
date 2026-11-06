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
    DOOR_CONTACT_STATUSES,
    DahuaDataUpdateCoordinator,
    door_status_is_contact,
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
    # handle_event logs the address before dispatching, and get_address reads
    # this. Only the CGI replay below needs it.
    c._address = "10.0.0.5"
    return c


def _listening(coordinator):
    key = coordinator.get_event_key("DoorStatus")
    coordinator.add_dahua_event_listener("DoorStatus", lambda: None)
    return key


def _event(payload):
    return {"Code": "DoorStatus", "Action": "Pulse", "Data": payload}


def _relay(status):
    return _event({"Relay": True, "Status": status})


def _contact(status):
    return _event({"DoorIndex": 0, "Status": status})


def test_the_reported_sequence_leaves_the_gate_open():
    """The five lines from the report, in their order. The gate is open from
    the third line on, and the relay's Close must not close it."""
    c = _vto()
    key = _listening(c)

    c.on_receive_vto_event(_relay("Close"))
    c.on_receive_vto_event(_contact("Close"))
    c.on_receive_vto_event(_contact("Open"))
    open_at = c._dahua_event_timestamp[key]
    assert open_at > 0, "the contact said Open"

    c.on_receive_vto_event(_relay("Close"))

    assert (
        c._dahua_event_timestamp[key] == open_at
    ), "the relay closing reported the gate shut while it was still swinging"

    # And the contact still closes it, which is the whole point of keeping it.
    c.on_receive_vto_event(_contact("Close"))
    assert c._dahua_event_timestamp[key] == 0


def test_the_reported_sequence_on_the_cgi_path_too():
    """The same five lines down the other transport, entered where that one
    enters: `handle_event`, with the lowercase `action` and `data` the CGI wire
    format parses to. A doorbell that does not answer `getDeviceClass` and is
    not in `is_doorbell`'s model list takes this path instead of DHIP, which is
    the #690 rebadge class, and three reads named only the DHIP spelling once
    before."""
    c = _vto()
    key = _listening(c)

    def cgi(payload):
        c.handle_event({"Code": "DoorStatus", "action": "Pulse", "data": payload})

    cgi({"Relay": True, "Status": "Close"})
    cgi({"DoorIndex": 0, "Status": "Close"})
    cgi({"DoorIndex": 0, "Status": "Open"})
    open_at = c._dahua_event_timestamp[key]
    assert open_at > 0, "the contact said Open"

    cgi({"Relay": True, "Status": "Close"})

    assert c._dahua_event_timestamp[key] == open_at

    cgi({"DoorIndex": 0, "Status": "Close"})
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


# --- a frame we cannot read is not evidence of a contact ----------------------
#
# rroller's review on #1169. `_door_contact_seen` is permanent, and it was being
# set by every DoorStatus that was not relay-shaped -- including the ones
# carrying nothing to read. So one malformed frame retired the relay fallback
# for the rest of the session, and a device with only the relay form to report
# sat wherever its sensor had got to until the entry was reloaded.

# A payload cannot express "the event had no payload key at all", which is one
# of the shapes, so the builders below take this instead of a payload.
ABSENT = object()

UNREADABLE_PAYLOADS = {
    "no payload key at all": ABSENT,
    "a null payload": None,
    "a truncated frame": "{'Relay':tr",
    "an empty payload": {},
    "a payload with no Status": {"DoorIndex": 0},
    "a Status this integration does not know": {"DoorIndex": 0, "Status": "Ajar"},
    "a Status that is not a string": {"DoorIndex": 0, "Status": []},
}


def _dhip(payload):
    """What the DHIP listener hands over: `Data`, and a capitalised `Action`."""
    event = {"Code": "DoorStatus", "Action": "Pulse"}
    if payload is not ABSENT:
        event["Data"] = payload
    return event


def _cgi(payload):
    """What the CGI wire format parses to: lowercase `data` and `action`."""
    event = {"Code": "DoorStatus", "action": "Pulse"}
    if payload is not ABSENT:
        event["data"] = payload
    return event


def _send_dhip(coordinator, payload):
    coordinator.on_receive_vto_event(_dhip(payload))


def _send_cgi(coordinator, payload):
    coordinator.handle_event(_cgi(payload))


# Both, every time. `_dispatch_event` is shared and the payload is spelled
# differently on each side, which is how three reads came to work on one
# transport only (see `event_payload`).
TRANSPORTS = {"dhip": _send_dhip, "cgi": _send_cgi}

RELAY_CLOSE = {"Relay": True, "Status": "Close"}
RELAY_OPEN = {"Relay": True, "Status": "Open"}
CONTACT_CLOSE = {"DoorIndex": 0, "Status": "Close"}


@pytest.mark.parametrize("transport", sorted(TRANSPORTS))
@pytest.mark.parametrize(
    "payload", UNREADABLE_PAYLOADS.values(), ids=list(UNREADABLE_PAYLOADS)
)
def test_an_unreadable_door_status_leaves_the_relay_driving(transport, payload):
    """The replay from the review: relay Close, then the frame that used to
    latch the flag, then relay Open. The relay has to still be able to open the
    sensor afterwards.

    Deliberately silent about what the unreadable frame itself does to the
    sensor. It writes closed, as it did before this branch and as it does on
    any other device, and that is a separate question from this one."""
    send = TRANSPORTS[transport]
    coordinator = _vto()
    key = _listening(coordinator)

    send(coordinator, RELAY_CLOSE)
    send(coordinator, payload)

    assert (
        coordinator._door_contact_seen is False
    ), "a frame with nothing to read was taken as proof of a door contact"

    send(coordinator, RELAY_OPEN)

    assert (
        coordinator._dahua_event_timestamp[key] > 0
    ), "the relay could no longer open its own sensor"


@pytest.mark.parametrize("transport", sorted(TRANSPORTS))
def test_a_readable_contact_status_still_retires_the_relay(transport):
    """The converse, on both transports. Narrowing what counts as a contact
    event must not stop the real ones counting."""
    send = TRANSPORTS[transport]
    coordinator = _vto()
    key = _listening(coordinator)

    send(coordinator, CONTACT_CLOSE)
    assert coordinator._door_contact_seen is True

    send(coordinator, {"DoorIndex": 0, "Status": "Open"})
    open_at = coordinator._dahua_event_timestamp[key]
    assert open_at > 0

    send(coordinator, RELAY_CLOSE)
    assert coordinator._dahua_event_timestamp[key] == open_at


# --- the two questions, asked of the helpers ----------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {"DoorIndex": 0, "Status": "Close"},
        {"DoorIndex": 0, "Status": "Open"},
        {"Status": "Open", "LocaleTime": "2026-10-31 19:49:46", "UTC": 1},
    ],
    ids=["the contact, closed", "the contact, open", "the VTO2000A payload"],
)
def test_a_measured_contact_payload_is_a_contact(payload):
    assert door_status_is_contact({"Data": payload})
    assert door_status_is_contact({"data": payload})


@pytest.mark.parametrize(
    "payload", UNREADABLE_PAYLOADS.values(), ids=list(UNREADABLE_PAYLOADS)
)
def test_nothing_unreadable_is_a_contact(payload):
    assert not door_status_is_contact(_dhip(payload))
    assert not door_status_is_contact(_cgi(payload))


def test_a_status_that_is_not_a_string_does_not_raise():
    """`in` on a set hashes what it is given, and the payload is whatever JSON
    arrived. A list or a dict there would raise TypeError inside the event
    handler, which takes the stream down for every event after it (#475)."""
    for status in ([], {}, 0, None, True):
        assert not door_status_is_contact({"Data": {"Status": status}})


def test_the_relay_is_never_also_a_contact():
    """`door_status_is_contact` answers its own question rather than relying on
    being asked second."""
    for status in DOOR_CONTACT_STATUSES:
        relay = {"Data": {"Relay": True, "Status": status}}
        assert door_status_is_relay(relay)
        assert not door_status_is_contact(relay)


def test_the_two_questions_are_not_a_negation_of_each_other():
    """Which is the whole fix: an event can be neither, and neither is what an
    unreadable frame is."""
    neither = [
        event
        for payload in UNREADABLE_PAYLOADS.values()
        for event in (_dhip(payload), _cgi(payload))
        if not door_status_is_relay(event) and not door_status_is_contact(event)
    ]

    assert len(neither) == 2 * len(UNREADABLE_PAYLOADS), (
        "every unreadable shape has to answer no to both questions, or the "
        "dispatch branch has nowhere to put it: %d of %d did"
        % (len(neither), 2 * len(UNREADABLE_PAYLOADS))
    )


def test_the_recognised_statuses_are_the_ones_the_sensor_can_act_on():
    """A guard on the constant. The sensor opens on exactly "Open" and closes
    on everything else, so a third spelling added here would be learned as a
    contact and then reported as closed."""
    assert DOOR_CONTACT_STATUSES == ("Close", "Open")
