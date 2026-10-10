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
from types import SimpleNamespace

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
    summary = dahua_utils.summarise_event(
        {
            "Code": "TrafficJunction",
            "JunctionDirection": "Obverse",
            "VehicleDirection": "Head",
        }
    )

    assert sorted(summary) == ["Code", "JunctionDirection", "VehicleDirection"]
    assert summary["JunctionDirection"] == "Obverse"


def test_a_number_plate_does_not():
    summary = dahua_utils.summarise_event({"Plate": "AB12CDE", "Lane": 0})

    assert summary["Plate"] == "<redacted>"
    assert summary["Lane"] == 0


def test_a_plate_nested_where_they_actually_arrive_does_not_either():
    summary = dahua_utils.summarise_event(
        {"data": {"TrafficCar": {"PlateNumber": "AB12CDE", "Category": "Car"}}}
    )

    car = summary["data"]["TrafficCar"]
    assert car["PlateNumber"] == "<redacted>"
    assert car["Category"] == "Car"


def test_cards_users_and_credentials_do_not():
    summary = dahua_utils.summarise_event(
        {
            "CardNo": "0099887766",
            "UserID": "17",
            "UserName": "Adrian",
            "Password": "hunter2",
            "Token": "abc",
        }
    )

    assert set(summary.values()) == {"<redacted>"}


def test_the_underscore_spelling_is_caught_too():
    """The field list is spelled without separators and the lookup strips them,
    so one entry covers a device using either spelling. None of these three has
    an underscored entry of its own."""
    for field, value in (
        ("raw_plate", "AB12CDE"),
        ("card_no", "0099887766"),
        ("user_id", "17"),
    ):
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


# --- and both doors into the coordinator have to use it ----------------------
#
# Everything above calls `_remember_event` directly, so it tested the capture
# and never the wiring. `on_receive_vto_event` did not call it at all, which
# made the buffer empty for every doorbell: the one device class whose
# BackKeyLight state numbers this exists to capture. #573 and #872 are both
# "which number did it send", and the dump field that should have answered
# always read as the device having sent nothing.


def _wired_coordinator():
    """A coordinator that can run either transport's entry point.

    Only the capture is under test, so the dispatch and the plate scan are
    stubbed: what matters is that both doors record the event on the way past.
    """
    c = object.__new__(DahuaDataUpdateCoordinator)
    c._address = "10.0.0.5"
    c._channel = 0
    c.get_device_name = lambda: "Front Door"
    c.hass = SimpleNamespace(bus=SimpleNamespace(fire=lambda *a, **k: None))
    c._handle_anpr_plate = lambda event: None
    c._dispatch_event = lambda event, action: None
    return c


def test_the_camera_path_remembers_what_arrived():
    """The control: the mechanism works, so the doorbell test below is about
    the wiring rather than about the capture."""
    c = _wired_coordinator()

    c.handle_event({"Code": "VideoMotion", "action": "Start"})

    assert [row["event"]["Code"] for row in c._recent_events] == ["VideoMotion"]


def test_the_doorbell_path_remembers_what_arrived():
    c = _wired_coordinator()

    c.on_receive_vto_event(
        {"Code": "BackKeyLight", "Action": "Pulse", "Data": {"State": 1}}
    )

    assert [row["event"]["Code"] for row in c._recent_events] == ["BackKeyLight"]


def test_the_doorbell_state_number_survives_into_the_buffer():
    """The question a doorbell report actually needs answered. 7 is a state the
    integration reads as "not a ring", which is exactly when somebody needs to
    see the number rather than be told the press did not happen."""
    c = _wired_coordinator()

    c.on_receive_vto_event(
        {"Code": "BackKeyLight", "Action": "Pulse", "Data": {"State": 7}}
    )

    assert c._recent_events[0]["event"]["Data"]["State"] == 7


def test_the_doorbell_event_is_stored_before_the_device_name_is_added():
    """`_remember_event`'s contract is what the device sent, not what we
    enriched it with, and `on_receive_vto_event` sets DeviceName on what used to
    be its first line. So the order is the thing to pin: a capture added after
    it would still fill the buffer and would quietly publish a field the device
    never sent."""
    c = _wired_coordinator()

    c.on_receive_vto_event({"Code": "BackKeyLight", "Action": "Pulse"})

    assert "DeviceName" not in c._recent_events[0]["event"]


def test_the_camera_event_is_stored_before_its_names_are_added():
    """The same contract on the other path, which already held it."""
    c = _wired_coordinator()

    c.handle_event({"Code": "VideoMotion", "action": "Start"})

    stored = c._recent_events[0]["event"]
    assert "name" not in stored
    assert "DeviceName" not in stored
