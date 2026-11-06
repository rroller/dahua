"""A door station with two call buttons says which one rang.

#1156, split out of #175 and #329, which both asked this and both waited years.
A station with two buttons produced one Doorbell Pressed sensor and one Doorbell
event entity, and neither said which button had been pressed, although the
identity was on the wire the whole time.

@altserg measured it on a two-button VTO3221D over five calls:

    | button | scenario     | Index | CallID |
    | 1      | not answered | 999   | 7      |
    | 1      | answered     | 999   | 2      |
    | 1      | rejected     | 999   | 5      |
    | 2      | not answered | 1999  | 6      |
    | 2      | rejected     | 1999  | 4      |

`Index` holds still across three outcomes on button 1, which is what rules out
the reading I could not rule out on the first two samples: a call counter. It is
the button. `CallID` is the field that varies, and it goes 7, 2, 5, 6, 4, so it
is a SIP slot rather than a count.

**The identity is not on the press.** `BackKeyLight` carries `Index: -1` on the
same device, and `BackKeyLight` is what becomes `DoorbellPressed`. So the ring is
not where this can be answered, and #1156's own suggestion of putting it on the
doorbell event entity would need the two events correlated by `CallID` across a
two-to-four millisecond gap. `CallNoAnswered` already has a sensor on every
doorbell, it fires with `Action: Start` when the call begins ringing rather than
when it goes unanswered, and it carries the index itself -- so the attributes go
there and nothing has to be correlated.
"""

from types import SimpleNamespace

import pytest

from custom_components.dahua import (
    CALL_BUTTON_BY_INDEX,
    CALL_BUTTON_EVENTS,
    DahuaDataUpdateCoordinator,
    call_button_index,
)

CODE = "CallNoAnswered"

# The payload of the first measured call, verbatim from @altserg's log except
# for the serial. What matters is that CallID is a string and Index is a number.
MEASURED = {
    "Action": "Start",
    "Code": CODE,
    "Data": {
        "CallID": "8",
        "IsEncryptedStream": False,
        "LocaleTime": "2026-09-26 13:40:31",
        "LockNum": 2,
        "SupportPaas": False,
        "TCPPort": 37777,
        "UTC": 1790426431.0,
    },
    "Index": 999,
    "deviceType": "VTO3221D",
}


def _vto(channel=0):
    """A coordinator with just enough of itself to run the event handler."""
    c = object.__new__(DahuaDataUpdateCoordinator)
    c._channel = channel
    c._dahua_event_timestamp = {}
    c._dahua_event_details = {}
    c._dahua_event_listeners = {}
    c.hass = SimpleNamespace(bus=SimpleNamespace(fire=lambda *a, **k: None))
    c.get_device_name = lambda: "Intercom Gate"
    c._handle_anpr_plate = lambda event: None
    c._address = "10.0.0.5"
    return c


def _listening(coordinator, code=CODE):
    """The details are stored past the no-listener skip, so one is needed."""
    coordinator.add_dahua_event_listener(code, lambda: None)
    return coordinator.get_event_key(code)


ABSENT = object()


def _dhip(index, call_id="8", code=CODE):
    """What the DHIP listener hands over: `Index` as a number, `Data`."""
    event = {"Code": code, "Action": "Start", "Data": {}}
    if call_id is not ABSENT:
        event["Data"]["CallID"] = call_id
    if index is not ABSENT:
        event["Index"] = index
    return event


def _cgi(index, call_id="8", code=CODE):
    """What the CGI wire format parses to: lowercase keys, `index` a string."""
    event = {"Code": code, "action": "Start", "data": {}}
    if call_id is not ABSENT:
        event["data"]["CallID"] = call_id
    if index is not ABSENT:
        event["index"] = index if index is ABSENT else str(index)
    return event


def _send_dhip(coordinator, index, call_id="8", code=CODE):
    coordinator.on_receive_vto_event(_dhip(index, call_id, code))


def _send_cgi(coordinator, index, call_id="8", code=CODE):
    coordinator.handle_event(_cgi(index, call_id, code))


# Both, every time. `_dispatch_event` is shared, the payload is spelled
# differently on each side, and the CGI index arrives as a string.
TRANSPORTS = {"dhip": _send_dhip, "cgi": _send_cgi}


# --- the measured device ------------------------------------------------------


def test_the_measured_payload_names_the_first_button():
    c = _vto()
    _listening(c)
    c.on_receive_vto_event(dict(MEASURED))

    details = c.get_event_details(CODE)
    assert details["call_button"] == 1
    assert details["call_button_index"] == 999
    assert details["call_id"] == "8"


@pytest.mark.parametrize("transport", sorted(TRANSPORTS))
@pytest.mark.parametrize("index,button", sorted(CALL_BUTTON_BY_INDEX.items()))
def test_each_measured_index_is_its_button(transport, index, button):
    """Driven from the mapping rather than from two literals, so a value added
    to it without evidence shows up here as a test with no measurement behind
    it."""
    c = _vto()
    _listening(c)
    TRANSPORTS[transport](c, index)

    assert c.get_event_details(CODE)["call_button"] == button


@pytest.mark.parametrize("transport", sorted(TRANSPORTS))
def test_the_two_buttons_are_told_apart(transport):
    """The whole point of the issue, in one assertion."""
    send = TRANSPORTS[transport]
    first, second = _vto(), _vto()
    _listening(first)
    _listening(second)

    send(first, 999)
    send(second, 1999)

    assert first.get_event_details(CODE)["call_button"] == 1
    assert second.get_event_details(CODE)["call_button"] == 2


# --- what is not claimed ------------------------------------------------------


@pytest.mark.parametrize("transport", sorted(TRANSPORTS))
def test_an_unmeasured_index_is_reported_without_a_button(transport):
    """2999 is what `button * 1000 - 1` predicts for a third button, and that
    arithmetic is a guess from two samples on one model. The raw index is still
    published, so an owner of such a station can see what their own buttons
    send and tell us."""
    c = _vto()
    _listening(c)
    TRANSPORTS[transport](c, 2999)

    details = c.get_event_details(CODE)
    assert details["call_button_index"] == 2999
    assert "call_button" not in details, (
        "a third button's index is not in the measured mapping, so no button "
        "number can be claimed for it"
    )


@pytest.mark.parametrize("transport", sorted(TRANSPORTS))
def test_a_later_unmeasured_index_does_not_keep_the_old_button(transport):
    """The details store replaces per code, and it had to: a stale `call_button`
    left beside a fresh index would be a false statement about which button
    rang, which is worse than saying nothing."""
    send = TRANSPORTS[transport]
    c = _vto()
    _listening(c)

    send(c, 999)
    assert c.get_event_details(CODE)["call_button"] == 1
    send(c, 2999)

    details = c.get_event_details(CODE)
    assert details["call_button_index"] == 2999
    assert "call_button" not in details, "button 1 outlived the event that said it"


@pytest.mark.parametrize("transport", sorted(TRANSPORTS))
def test_a_station_that_reports_no_button_claims_none(transport):
    """`Index: -1` is what the same device puts on the press event. It means
    "not a button", exactly as `door_index` reads it as "not a door"."""
    c = _vto()
    _listening(c)
    TRANSPORTS[transport](c, -1)

    details = c.get_event_details(CODE)
    assert "call_button" not in details
    assert details["call_button_index"] == -1


# --- a payload is whatever arrived -------------------------------------------

UNREADABLE_INDEXES = {
    "no index at all": ABSENT,
    "a null index": None,
    "an index that is not a number": "button two",
    "an index that is a list": [],
    "an index that is a dict": {},
    "an index that is a bool": True,
}


@pytest.mark.parametrize(
    "index", UNREADABLE_INDEXES.values(), ids=list(UNREADABLE_INDEXES)
)
def test_nothing_unreadable_reaches_the_mapping(index):
    """`CALL_BUTTON_BY_INDEX.get([])` raises TypeError, and this runs inside the
    event handler, where an exception takes the stream down for every event
    after it (#475). So the coercion happens before the lookup, not after."""
    assert call_button_index({"Index": index} if index is not ABSENT else {}) is None


@pytest.mark.parametrize(
    "index", UNREADABLE_INDEXES.values(), ids=list(UNREADABLE_INDEXES)
)
def test_an_unreadable_index_does_not_stop_the_event(index):
    """Through the real handler on the transport that sends raw JSON, because
    the guard is only worth having if the event survives it."""
    c = _vto()
    key = _listening(c)
    event = {"Code": CODE, "Action": "Start", "Data": {"CallID": "8"}}
    if index is not ABSENT:
        event["Index"] = index

    c.on_receive_vto_event(event)

    assert c._dahua_event_timestamp[key] > 0, "the sensor never saw the call"
    assert "call_button" not in c.get_event_details(CODE)


def test_a_bool_index_is_not_button_one():
    """`True == 1` in Python, and `isinstance(True, int)` is also true, so a
    payload of `Index: true` would have read as the index 1 without the explicit
    check. Not button 1 either way, since 1 is not in the mapping, but it must
    not be published as the index the device sent."""
    assert call_button_index({"Index": True}) is None
    assert call_button_index({"Index": False}) is None


def test_a_truncated_payload_still_names_the_button():
    """`event_payload` turns a non-dict payload into {}, so CallID goes missing
    while the index, which is outside the payload, survives. The button is the
    answer the issue asked for, so it must not depend on the rest parsing."""
    c = _vto()
    _listening(c)
    c.on_receive_vto_event(
        {"Code": CODE, "Action": "Start", "Data": "{'CallID':", "Index": 1999}
    )

    details = c.get_event_details(CODE)
    assert details["call_button"] == 2
    assert "call_id" not in details


# --- scope --------------------------------------------------------------------


def test_only_the_call_event_reads_its_index_as_a_button():
    """Every event carries an Index and on almost all of them it is a channel.
    A VideoMotion on channel 1 must not report call button attributes."""
    c = _vto()
    _listening(c, "VideoMotion")
    c.on_receive_vto_event({"Code": "VideoMotion", "Action": "Start", "Index": 1})

    details = c.get_event_details("VideoMotion")
    assert "call_button" not in details
    assert "call_button_index" not in details


def test_the_press_event_is_not_where_the_button_is_read():
    """BackKeyLight carries Index -1 on the measured device, so including it
    would publish a button attribute that never says anything."""
    assert "BackKeyLight" not in CALL_BUTTON_EVENTS
    assert "DoorbellPressed" not in CALL_BUTTON_EVENTS
    assert CALL_BUTTON_EVENTS == frozenset({CODE})


def test_the_mapping_holds_only_what_was_measured():
    """Two buttons were measured. A third entry here would be arithmetic, which
    is what #175 went wrong on twice."""
    assert CALL_BUTTON_BY_INDEX == {999: 1, 1999: 2}


def test_the_sensor_that_carries_this_exists_on_every_doorbell():
    """The attributes are only reachable through an entity. This one is built
    unconditionally for a doorbell, so nothing has to be enabled first."""
    import inspect

    from custom_components.dahua import binary_sensor

    source = inspect.getsource(binary_sensor.async_setup_entry)
    assert 'DahuaEventSensor(coordinator, entry, "CallNoAnswered")' in source, (
        "the CallNoAnswered sensor is where #1156's answer is published; if it "
        "moved or became conditional, this test is the place to say so"
    )
