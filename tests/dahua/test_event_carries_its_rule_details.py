"""An IVS/smart event carries which rule tripped, its direction and object (#373).

A tripwire binary sensor turning on says something crossed a line; it cannot say it
was the driveway rule not the back gate, or a person not a car. The event already
carries all of it, and this keeps the triggering event's `Name`, `Direction`,
`RuleId` and `Object.ObjectType` so the sensor can expose them as attributes.

Driven through the real `_dispatch_event` and `_extract_event_details` with the same
coordinator double as test_which_codes_an_event_becomes. The field shapes are the ones
captured from a DHI-NVR5464: rule names like "Pool Entry", Direction LeftToRight /
RightToLeft, Object.ObjectType Human / Vehicle / Unknown, and RuleId (also sent as
RuleID on some events).
"""

from custom_components.dahua import DahuaDataUpdateCoordinator

CHANNEL = 3


def _coordinator(*listening):
    """A coordinator listening for exactly the codes named, with empty stores."""
    c = object.__new__(DahuaDataUpdateCoordinator)
    c._channel = CHANNEL
    c._dahua_event_listeners = {
        c.get_event_key(code): [lambda: None] for code in listening
    }
    c._dahua_event_timestamp = {}
    c._dahua_event_details = {}
    c._events_without_listener = None
    return c


# --- the extractor, against real payload shapes and the edges ----------------


def test_extract_pulls_name_direction_and_drops_unknown():
    c = _coordinator()
    details = c._extract_event_details(
        {
            "data": {
                "Name": "Rule1",
                "Direction": "RightToLeft",
                "RuleId": 1,
                "Object": {"ObjectType": "Unknown"},
            }
        }
    )
    assert details == {"rule_name": "Rule1", "rule_id": 1, "direction": "RightToLeft"}


def test_extract_reads_the_dhip_capital_casing_and_ruleid():
    c = _coordinator()
    assert c._extract_event_details(
        {"Data": {"RuleID": 2, "Object": {"ObjectType": "Vehicle"}}}
    ) == {"rule_id": 2, "object_type": "Vehicle"}


def test_extract_survives_a_null_object():
    c = _coordinator()
    assert c._extract_event_details({"data": {"Object": None, "RuleId": 1}}) == {
        "rule_id": 1
    }


def test_extract_of_a_truncated_payload_is_empty():
    c = _coordinator()
    assert c._extract_event_details({"data": '{"Name": "Rul'}) == {}


def test_extract_of_plain_motion_is_empty():
    c = _coordinator()
    assert (
        c._extract_event_details({"data": {"Id": [0], "RegionName": ["Region1"]}}) == {}
    )


# --- the details survive the dispatch and come back out ----------------------


def test_a_crossline_event_stores_its_details():
    c = _coordinator("CrossLineDetection")
    c._dispatch_event(
        {
            "Code": "CrossLineDetection",
            "data": {
                "Name": "Pool Entry",
                "Direction": "LeftToRight",
                "Object": {"ObjectType": "Human"},
            },
        },
        "Start",
    )
    assert c.get_event_details("CrossLineDetection") == {
        "rule_name": "Pool Entry",
        "direction": "LeftToRight",
        "object_type": "Human",
    }


def test_details_reach_the_derived_smart_sensor_too():
    """A human CrossLine also fires SmartMotionHuman; both sensors get the detail."""
    c = _coordinator("CrossLineDetection", "SmartMotionHuman")
    c._dispatch_event(
        {
            "Code": "CrossLineDetection",
            "data": {
                "Name": "Driveway",
                "Direction": "RightToLeft",
                "Object": {"ObjectType": "Human"},
            },
        },
        "Start",
    )
    assert c.get_event_details("SmartMotionHuman")["rule_name"] == "Driveway"
    assert c.get_event_details("CrossLineDetection")["object_type"] == "Human"


def test_a_plain_motion_event_stores_nothing():
    c = _coordinator("VideoMotion")
    c._dispatch_event(
        {"Code": "VideoMotion", "data": {"Id": [0], "RegionName": ["Region1"]}}, "Start"
    )
    assert c.get_event_details("VideoMotion") == {}


def test_details_are_empty_before_any_event():
    c = _coordinator("CrossLineDetection")
    assert c.get_event_details("CrossLineDetection") == {}


def test_a_detail_less_event_does_not_wipe_a_kept_one():
    """A later plain event for the same code keeps the last rule it reported,
    the way the timestamp persists rather than blanking."""
    c = _coordinator("CrossLineDetection")
    c._dispatch_event(
        {
            "Code": "CrossLineDetection",
            "data": {"Name": "Driveway", "Object": {"ObjectType": "Vehicle"}},
        },
        "Start",
    )
    c._dispatch_event({"Code": "CrossLineDetection", "data": {}}, "Stop")
    assert c.get_event_details("CrossLineDetection")["rule_name"] == "Driveway"
