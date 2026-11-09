"""What one arriving event turns into, and who has to be listening for it.

`translate_event_code` is the only place that decides a CrossLine or CrossRegion event
should *also* fire a Smart Motion sensor. It is the middle of the path every report about
missing Human and Vehicle sensors runs through (#728, #767, #825), and none of its
thirty-two statements was executed by the suite.

The rule it implements is not obvious from reading it, so stated plainly: the original
code is dispatched only if something is listening for it, the derived Smart Motion code
is dispatched if something is listening for *that*, and if neither is listening the
derived code is returned anyway so the caller has something to look up and discard. That
last branch is why an event can be translated for a sensor nobody has, which reads like a
bug until you notice `_dispatch_event` drops it a moment later.

The failure that prompted these tests is the one below about a null `Object`.
`data.get("Object", {})` returns `None` when the key is present and null, not the default,
so the next `.get` raised. The device does send nulls -- `"Track": None` is in this
module's own example payload -- and a CrossLine event that detected no object is exactly
when it would. Since #706 the shared stream catches that per coordinator, so the cost is
a dropped event and a warning per occurrence rather than a dead stream, which is quiet
enough to have gone unnoticed but is still a motion event that never fires.
"""

import pytest

from custom_components.dahua import DahuaDataUpdateCoordinator

CHANNEL = 3


def _coordinator(*listening):
    """A coordinator that is listening for exactly the codes named."""
    c = object.__new__(DahuaDataUpdateCoordinator)
    c._channel = CHANNEL
    c._dahua_event_listeners = {
        c.get_event_key(code): [lambda: None] for code in listening
    }
    return c


def _crossline(object_type=None, code="CrossLineDetection", key="data"):
    event = {"Code": code, "action": "Start"}
    if object_type is not None:
        event[key] = {"Object": {"ObjectType": object_type}}
    return event


# --- the derived Smart Motion codes -----------------------------------------


def test_a_human_reaches_a_smart_motion_sensor():
    """The case every missing-sensor report is about: the user selected
    SmartMotionHuman and nothing else, and the device only ever sends
    CrossLineDetection."""
    coordinator = _coordinator("SmartMotionHuman")

    assert coordinator.translate_event_code(_crossline("Human")) == ["SmartMotionHuman"]


def test_a_vehicle_reaches_its_own_sensor():
    coordinator = _coordinator("SmartMotionVehicle")

    assert coordinator.translate_event_code(_crossline("Vehicle")) == [
        "SmartMotionVehicle"
    ]


def test_both_fire_when_both_are_listened_for():
    """Not one or the other. Somebody with both sensors wants both, and the
    original comes first so the raw event is not reordered behind a derived one."""
    coordinator = _coordinator("CrossLineDetection", "SmartMotionHuman")

    assert coordinator.translate_event_code(_crossline("Human")) == [
        "CrossLineDetection",
        "SmartMotionHuman",
    ]


def test_a_smart_motion_code_nobody_wants_is_not_added():
    """Listening for the raw event only. Adding the derived code here would make
    `_dispatch_event` look up a sensor that does not exist on every event."""
    coordinator = _coordinator("CrossLineDetection")

    assert coordinator.translate_event_code(_crossline("Human")) == [
        "CrossLineDetection"
    ]


@pytest.mark.parametrize(
    "object_type, derived",
    [
        ("Human", "SmartMotionHuman"),
        ("Vehicle", "SmartMotionVehicle"),
    ],
)
def test_with_nothing_listening_the_derived_code_is_still_returned(
    object_type, derived
):
    """The branch that reads like a bug. Returning the derived code when nothing
    is listening gives the caller something to look up and discard, and is how the
    function avoids returning an empty list.

    Both object types, because the two are separate copies of the same branch and
    only the Human one was being exercised. A copy nothing reaches is a copy that
    can be broken without a red test.
    """
    coordinator = _coordinator()

    assert coordinator.translate_event_code(_crossline(object_type)) == [derived]


@pytest.mark.parametrize("object_type", ["Human", "human", "HUMAN"])
def test_the_object_type_is_matched_case_insensitively(object_type):
    """Firmware disagrees on the casing, and the comparison is against lowercase."""
    coordinator = _coordinator("SmartMotionHuman")

    assert coordinator.translate_event_code(_crossline(object_type)) == [
        "SmartMotionHuman"
    ]


def test_cross_region_translates_the_same_way():
    """Two codes, one rule. They are checked together in the source and so are
    tested together, since the second one having the behaviour says nothing about
    the first."""
    coordinator = _coordinator("SmartMotionVehicle")

    assert coordinator.translate_event_code(
        _crossline("Vehicle", code="CrossRegionDetection")
    ) == ["SmartMotionVehicle"]


def test_the_dhip_spelling_of_the_payload_is_read_too():
    """The CGI stream parses to `data` and DHIP sends `Data`. A device on the
    other transport would derive nothing at all if only one were read."""
    coordinator = _coordinator("SmartMotionHuman")

    assert coordinator.translate_event_code(_crossline("Human", key="Data")) == [
        "SmartMotionHuman"
    ]


# --- payloads that are not what they should be ------------------------------


def test_an_event_with_a_null_object_is_not_a_crash():
    """The regression test. `"Object": null` is the key being present and empty,
    which `.get("Object", {})` does not defend against, so this raised
    AttributeError out of translate_event_code. Since #706 the shared stream
    catches it per coordinator, so the cost is a dropped event and a warning for
    every one of them rather than a dead stream.
    """
    coordinator = _coordinator("CrossLineDetection")
    event = {
        "Code": "CrossLineDetection",
        "action": "Start",
        "data": {"Object": None, "RuleId": 1},
    }

    assert coordinator.translate_event_code(event) == ["CrossLineDetection"]


def test_an_event_with_no_object_at_all_still_dispatches_the_raw_code():
    """A CrossLine rule that fired without classifying anything. There is no Smart
    Motion code to derive, and the raw event is still an event."""
    coordinator = _coordinator("CrossLineDetection")
    event = {"Code": "CrossLineDetection", "action": "Start", "data": {"RuleId": 1}}

    assert coordinator.translate_event_code(event) == ["CrossLineDetection"]


def test_a_truncated_payload_does_not_stop_the_dispatch():
    """#475. `parse_event` leaves the raw string here when the JSON did not parse,
    and `.get` on a string raises. A payload we could not read is a payload with no
    ObjectType, not a reason to stop listening."""
    coordinator = _coordinator("CrossLineDetection")
    event = {
        "Code": "CrossLineDetection",
        "action": "Start",
        "data": '{"Object": {"ObjectT',
    }

    assert coordinator.translate_event_code(event) == ["CrossLineDetection"]


def test_an_object_type_of_null_is_not_a_crash_either():
    """The same shape one level down."""
    coordinator = _coordinator("CrossLineDetection")
    event = {
        "Code": "CrossLineDetection",
        "action": "Start",
        "data": {"Object": {"ObjectType": None}},
    }

    assert coordinator.translate_event_code(event) == ["CrossLineDetection"]


def test_an_unrecognised_object_type_derives_nothing():
    """`Unknown` is what the example payload in this module carries, so it is the
    common case rather than an odd one, and it must not become a Smart Motion
    event."""
    coordinator = _coordinator("CrossLineDetection")

    assert coordinator.translate_event_code(_crossline("Unknown")) == [
        "CrossLineDetection"
    ]


# --- the doorbell codes -----------------------------------------------------


def test_a_vto_call_state_becomes_a_doorbell_press():
    coordinator = _coordinator()
    event = {"Code": "BackKeyLight", "Action": "Pulse", "Data": {"State": 1}}

    assert coordinator.translate_event_code(event) == ["DoorbellPressed"]


def test_an_amcrest_doorbell_uses_a_different_code_for_the_same_thing():
    coordinator = _coordinator()
    event = {"Code": "PhoneCallDetect", "Action": "Pulse", "Data": {"State": 1}}

    assert coordinator.translate_event_code(event) == ["DoorbellPressed"]


def test_an_unlock_carries_its_own_code_as_well():
    """State 8 is the only signal a door lock can confirm an unlock from: the
    device sends no AccessControl event for it. Collapsing every call state to
    DoorbellPressed threw that away."""
    coordinator = _coordinator()
    event = {"Code": "BackKeyLight", "Action": "Pulse", "Data": {"State": 8}}

    assert coordinator.translate_event_code(event) == [
        "DoorbellPressed",
        "DoorUnlocked",
    ]


def test_a_failed_unlock_is_told_apart_from_a_successful_one():
    coordinator = _coordinator()
    event = {"Code": "BackKeyLight", "Action": "Pulse", "Data": {"State": 9}}

    assert coordinator.translate_event_code(event) == [
        "DoorbellPressed",
        "DoorUnlockFailed",
    ]


def test_a_call_state_with_nothing_extra_is_just_the_press():
    """State 5 is one of the quiet ones (#872, #883). It must not invent a code."""
    coordinator = _coordinator()
    event = {"Code": "BackKeyLight", "Action": "Pulse", "Data": {"State": 5}}

    assert coordinator.translate_event_code(event) == ["DoorbellPressed"]


# --- and everything else passes straight through ----------------------------


@pytest.mark.parametrize("code", ["VideoMotion", "AlarmLocal", "FaceDetection"])
def test_an_ordinary_code_is_returned_as_itself(code):
    coordinator = _coordinator()

    assert coordinator.translate_event_code({"Code": code}) == [code]


def test_an_event_with_no_code_does_not_raise():
    """`Code` is read with a default, and an event without one still has to come
    back as something the caller can iterate."""
    coordinator = _coordinator()

    assert coordinator.translate_event_code({}) == [""]


# --- a door station that reports the whole call on one code (#1121) ----------
#
# @DrSpaldo captured a single call press on a DHI-ASI7214S and a
# DHI-ASI6214S-PW. One press is four `_DoTalkAction_` events sharing a CallID:
#
#     Action: Invite                                 <- the press
#     Action: Answer    HangupReason: AnswerByPhone
#     Action: Hangup    HangupReason: HangupByPhone
#     Action: Hangup    HangupReason: HangupByVTO
#
# A sensor built straight from the code would come on four times for one
# caller, three of them after the visitor had gone, which is why only Invite
# is the press and the rest fall through as the raw code nothing subscribes to.


def _talk(action=None, key="data"):
    """A `_DoTalkAction_` event. `key` is the payload casing: the CGI wire
    format parses to `data`, which is what @DrSpaldo captured, and DHIP sends
    `Data`. Both have to read the same."""
    event = {"Code": "_DoTalkAction_", "action": "Pulse", "index": "0"}
    payload = {"CallID": "20261026124301@838917@", "CallSrcMask": 4}
    if action is not None:
        payload["Action"] = action
    event[key] = payload
    return event


@pytest.mark.parametrize("key", ["data", "Data"])
def test_an_invite_is_a_doorbell_press(key):
    coordinator = _coordinator()

    assert coordinator.translate_event_code(_talk("Invite", key)) == ["DoorbellPressed"]


@pytest.mark.parametrize("action", ["Answer", "Hangup"])
def test_the_rest_of_the_call_is_not_a_press(action):
    """Returned as the raw code, which is not in ALL_EVENTS, so nothing is
    listening and `_dispatch_event` drops it where it drops every other
    unselected code."""
    coordinator = _coordinator()

    assert coordinator.translate_event_code(_talk(action)) == ["_DoTalkAction_"]


def test_a_call_event_with_no_action_is_not_a_press():
    """Only Invite is. Anything this does not recognise must not become one,
    because the sensor it would raise is somebody's front door."""
    coordinator = _coordinator()

    assert coordinator.translate_event_code(_talk()) == ["_DoTalkAction_"]


def test_an_unreadable_payload_does_not_raise():
    """A truncated event leaves the payload as a string, and `.get` on a
    string raises out of the whole stream. #475 is what that costs."""
    coordinator = _coordinator()
    event = {"Code": "_DoTalkAction_", "data": '{"Action": "Inv'}

    assert coordinator.translate_event_code(event) == ["_DoTalkAction_"]


def test_the_room_number_is_not_read():
    """RoomNo is the room the station is calling, set per device in its web UI,
    so it names the destination rather than the press. Two devices calling the
    same room must give the same answer, and Answer carries no RoomNo at all.
    """
    coordinator = _coordinator()
    front_door = _talk("Invite")
    front_door["data"]["RoomNo"] = "889"
    gate = _talk("Invite")
    gate["data"]["RoomNo"] = "888"

    assert coordinator.translate_event_code(front_door) == ["DoorbellPressed"]
    assert coordinator.translate_event_code(gate) == ["DoorbellPressed"]
