"""The last few events, in diagnostics, so nobody has to run curl.

Across the tracker the same thing is missing over and over: what did the device
actually send? Which `Code`, which `action`, which field carries the state, the
direction or the object type. #572, #573, #403, #329, #385, #456, #276, #288,
#336 and #432 are all stalled on exactly that, and the answer has always been
"run this curl and paste the output".

That fails the people who need it most. #713's reporter read a curl command as a
request to modify the integration's code, which is a reasonable reading if you
are not a programmer. Diagnostics is a button in the user interface.

**This publishes less than the status quo, not more.** People already paste raw
events into public issues by hand, plate numbers and all. Here the field names
survive, because that is the question, and the values are redacted or truncated.

The capture itself is deliberately dumb. It runs on the event stream, where an
unguarded exception takes every camera on the host down until it reconnects
(#705, #706), so it only appends to a bounded deque. All the work that could go
wrong happens when diagnostics is asked for.
"""
from collections import deque

from custom_components.dahua import (
    RECENT_EVENT_COUNT,
    DahuaDataUpdateCoordinator,
    dahua_utils,
)


def _coordinator():
    """A coordinator with nothing set but what the capture touches."""
    return object.__new__(DahuaDataUpdateCoordinator)


# --- the capture -------------------------------------------------------------

def test_an_event_is_remembered():
    c = _coordinator()

    c._remember_event({"Code": "VideoMotion", "action": "Start"})

    assert len(c._recent_events) == 1
    assert c._recent_events[0]["event"]["Code"] == "VideoMotion"


def test_the_buffer_is_bounded():
    """A device that fires all day must not grow the buffer all day."""
    c = _coordinator()

    for index in range(RECENT_EVENT_COUNT * 4):
        c._remember_event({"Code": "VideoMotion", "index": index})

    assert len(c._recent_events) == RECENT_EVENT_COUNT


def test_the_oldest_events_are_the_ones_dropped():
    c = _coordinator()

    for index in range(RECENT_EVENT_COUNT + 3):
        c._remember_event({"index": index})

    kept = [row["event"]["index"] for row in c._recent_events]
    assert kept[-1] == RECENT_EVENT_COUNT + 2
    assert 0 not in kept


def test_a_copy_is_kept_not_the_caller_s_dict():
    """handle_event adds the device name after this runs; the buffer must not
    quietly change underneath, and must not hold the event alive either."""
    c = _coordinator()
    event = {"Code": "VideoMotion"}

    c._remember_event(event)
    event["Code"] = "changed afterwards"

    assert c._recent_events[0]["event"]["Code"] == "VideoMotion"


def test_the_capture_works_on_a_coordinator_that_has_never_seen_one():
    """getattr with a default, because most tests build these with
    object.__new__ and a diagnostic must never be what breaks one."""
    c = _coordinator()
    assert not hasattr(c, "_recent_events")

    c._remember_event({"Code": "AlarmLocal"})

    assert len(c._recent_events) == 1


def test_an_existing_buffer_is_reused():
    c = _coordinator()
    c._recent_events = deque(maxlen=RECENT_EVENT_COUNT)
    c._recent_events.append({"seconds_ago_at_capture": 0, "event": {"Code": "old"}})

    c._remember_event({"Code": "new"})

    assert [row["event"]["Code"] for row in c._recent_events] == ["old", "new"]


# --- what gets published -----------------------------------------------------

def test_field_names_survive():
    """The whole point: the question is which field carries the answer."""
    summary = dahua_utils.summarise_event({
        "Code": "TrafficJunction",
        "JunctionDirection": "Obverse",
        "VehicleDirection": "Head",
    })

    assert sorted(summary) == ["Code", "JunctionDirection", "VehicleDirection"]
    assert summary["JunctionDirection"] == "Obverse"


def test_a_number_plate_does_not():
    summary = dahua_utils.summarise_event({"Plate": "AB12CDE", "Lane": 0})

    assert summary["Plate"] == "<redacted>"
    assert summary["Lane"] == 0


def test_a_plate_nested_where_they_actually_arrive_does_not_either():
    summary = dahua_utils.summarise_event(
        {"data": {"TrafficCar": {"PlateNumber": "AB12CDE", "Category": "Car"}}})

    car = summary["data"]["TrafficCar"]
    assert car["PlateNumber"] == "<redacted>"
    assert car["Category"] == "Car"


def test_cards_users_and_credentials_do_not():
    summary = dahua_utils.summarise_event({
        "CardNo": "0099887766",
        "UserID": "17",
        "UserName": "Adrian",
        "Password": "hunter2",
        "Token": "abc",
    })

    assert set(summary.values()) == {"<redacted>"}


def test_the_underscore_spelling_is_caught_too():
    """The field list is spelled without separators and the lookup strips them,
    so one entry covers a device using either spelling. None of these three has
    an underscored entry of its own."""
    for field, value in (("raw_plate", "AB12CDE"),
                         ("card_no", "0099887766"),
                         ("user_id", "17")):
        assert dahua_utils.summarise_event({field: value})[field] == "<redacted>", field


def test_a_long_value_is_truncated_rather_than_dropped():
    summary = dahua_utils.summarise_event({"Blob": "x" * 500})

    assert summary["Blob"].endswith("<truncated>")
    assert len(summary["Blob"]) < 200


def test_a_long_list_keeps_its_start_and_says_what_it_dropped():
    summary = dahua_utils.summarise_event({"BoundingBox": list(range(40))})

    assert summary["BoundingBox"][0] == 0
    assert summary["BoundingBox"][-1].endswith("more>")


def test_a_short_value_is_left_exactly_as_it_was():
    assert dahua_utils.summarise_event({"action": "Start"})["action"] == "Start"


def test_something_that_is_not_a_dict_does_not_raise():
    for value in (None, 0, "text", [], {}):
        dahua_utils.summarise_event(value)


def test_a_deeply_nested_event_stops_rather_than_recursing_for_ever():
    deepest = {"a": {}}
    node = deepest["a"]
    for _ in range(40):
        node["a"] = {}
        node = node["a"]

    summary = dahua_utils.summarise_event(deepest)

    flat = repr(summary)
    assert "too deep" in flat
