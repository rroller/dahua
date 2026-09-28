"""A payload we could not parse must not kill the event stream.

`parse_event` converts the `data=` payload from JSON text into a dict, and
silently leaves the raw string when that fails. It fails when the device's
payload arrives truncated -- the case #678 fixed for ANPR, reported for
CrossLineDetection in #475, whose log ends mid-JSON:

    'data': '{ ... "uuid" : "2401'

`translate_event_code` then did:

    data = event.get("data", event.get("Data", {}))
    object_type = data.get("Object", {}).get("ObjectType", "").lower()

and `.get()` on a string raises AttributeError. Nothing catches it:
handle_event does not, on_receive does not, and the stream loop wraps its
on_receive call in try/finally with no handler. So one malformed CrossLine
event took the event stream down for **every channel on that host**, not just
the camera that sent it.
"""

import json

from custom_components.dahua.dahua_utils import parse_event


def _block(payload):
    return ("--myboundary\r\n"
            "Content-Type: text/plain\r\n"
            "Content-Length: 0\r\n\r\n"
            "Code=CrossLineDetection;action=Start;index=1;data=" + payload + "\r\n")


FULL = json.dumps({"Name": "Brievenbus", "RuleID": 5,
                   "Object": {"ObjectType": "Human"}})


# --- the good case still works ----------------------------------------------

def test_a_whole_payload_is_parsed_into_a_dict():
    event = parse_event(_block(FULL))[0]

    assert isinstance(event["data"], dict)
    assert event["data"]["Name"] == "Brievenbus"
    assert event["data"]["Object"]["ObjectType"] == "Human"


# --- and a truncated one no longer takes the stream with it -----------------

def test_a_truncated_payload_is_left_as_text_not_dropped():
    """#475's own shape: the JSON stops partway through."""
    event = parse_event(_block(FULL[:len(FULL) // 2]))[0]

    assert event["Code"] == "CrossLineDetection"
    assert isinstance(event["data"], str), "the raw text is kept, not discarded"


def test_reading_a_truncated_payload_the_way_the_handler_does():
    """What translate_event_code does, against what parse_event produced."""
    event = parse_event(_block(FULL[:len(FULL) // 2]))[0]

    data = event.get("data", event.get("Data", {}))
    if not isinstance(data, dict):
        data = {}

    # The line that used to raise AttributeError and end the stream.
    assert data.get("Object", {}).get("ObjectType", "").lower() == ""


def test_the_unguarded_version_is_what_raised():
    """Kept so the reason for the isinstance check cannot be optimised away."""
    event = parse_event(_block(FULL[:len(FULL) // 2]))[0]
    data = event.get("data", event.get("Data", {}))

    try:
        data.get("Object", {})
    except AttributeError:
        return
    raise AssertionError("expected AttributeError from .get() on a string")
