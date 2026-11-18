"""Who a FaceRecognition event says it saw (#1089).

`FaceRecognition` was only a binary sensor: on while a face was being seen, and
nothing about whose. The payloads here are @Mark79864's captures from #1089,
trimmed to the fields that matter. The same shape came from an NVR5-XI/PRO, an
NVR4xxx-EI, and a channel whose video was an OBS ONVIF stream, so the recorder
does the analysis and the shape does not depend on the camera.

Four shapes, and each is treated differently:

    recognised          Candidates[0].Person: Name, GroupName, GroupType ...
    stranger            Candidates [], plus a top-level Passerby block
    face, no stranger   Candidates [], no Passerby
    Stop                action=Stop, Candidates [], Passerby again

And one fact the code must not lose: the issue body said group 1 is the
passerby database, and the reporter's own capture showed GroupID "1" named
"Test" of type BlackListDB. GroupType carries the meaning; the id is an id.
"""

import json
from types import SimpleNamespace

import pytest

from custom_components.dahua import sensor as sensor_module
from custom_components.dahua.const import EVENT_DAHUA_FACE_RECOGNITION
from custom_components.dahua.coordinator import DahuaDataUpdateCoordinator
from custom_components.dahua.dahua_utils import extract_face_data
from custom_components.dahua.sensor import DahuaFaceRecognitionSensor

from . import adds_entities
from .test_entity_categories import _setup, _setup_coordinator  # noqa: F401
from .test_entity_categories import _skip_ha_plumbing  # noqa: F401


def _person(name, group_id, group_name, group_type="BlackListDB", uid="20"):
    return {
        "Name": name,
        "GroupID": group_id,
        "GroupName": group_name,
        "GroupType": group_type,
        "UID": uid,
        "PersonID": float(uid),
        "Sex": "Male",
        # A passerby block of its own sits inside every recognised Person in
        # the captures. It is not the top-level one that marks a stranger.
        "CustomPasserbyInfo": {"OccurrenceNumber": 0},
    }


def _event(candidates=(), passerby=None, action="Start", age=27, index=6):
    data = {
        "Candidates": list(candidates),
        "Class": "FaceAnalysis",
        "Code": "FaceRecognition",
        "Face": {"Age": age, "ObjectType": "HumanFace", "Sex": "Man"},
        "Object": {"Action": "Appear", "ObjectType": "HumanFace"},
    }
    if passerby is not None:
        data["Passerby"] = passerby
    return {
        "Code": "FaceRecognition",
        "action": action,
        "index": str(index),
        "data": data,
    }


MARK = _event([{"Person": _person("Mark", "2", "Family"), "Similarity": 100}])
SIMON = _event(
    [{"Person": _person("Simon", "3", "Friends", uid="31"), "Similarity": 86}]
)
STRANGER = _event(passerby={"GroupID": "1", "UID": "127"}, age=36)
FACE_ONLY = _event()
STOP = _event(passerby={"GroupID": "1", "UID": "132"}, action="Stop")
# The capture that disproved "group 1 is the passerby database".
GROUP_ONE_NAMED_TEST = _event(
    [{"Person": _person("Ann", "1", "Test", uid="5"), "Similarity": 92}]
)


# --- the parser ---------------------------------------------------------------


def test_a_recognised_face_names_the_person_and_where_they_matched():
    face = extract_face_data(MARK)

    assert face == {
        "recognized": True,
        "name": "Mark",
        "similarity": 100,
        "group_name": "Family",
        "group_type": "BlackListDB",
        "group_id": "2",
        "person_id": "20",
        "stranger": False,
        "passerby_id": None,
        "object_type": "HumanFace",
        "age": 27,
        "sex": "Man",
    }


def test_a_stranger_is_one_because_of_the_passerby_block():
    face = extract_face_data(STRANGER)

    assert face["recognized"] is False
    assert face["name"] is None
    assert face["stranger"] is True
    assert face["passerby_id"] == "127"
    assert face["age"] == 36


def test_a_face_with_stranger_mode_off_is_neither():
    face = extract_face_data(FACE_ONLY)

    assert face["recognized"] is False
    assert face["stranger"] is False
    assert face["passerby_id"] is None


def test_group_one_is_whatever_the_device_says_it_is():
    """The neighbouring-but-wrong case. A parser that read group 1 as the
    passerby database would file Ann, recognised in a database the user named
    Test, as a stranger."""
    face = extract_face_data(GROUP_ONE_NAMED_TEST)

    assert face["recognized"] is True
    assert face["stranger"] is False
    assert face["name"] == "Ann"
    assert face["group_id"] == "1"
    assert face["group_name"] == "Test"
    assert face["group_type"] == "BlackListDB"


def test_a_match_wins_over_a_passerby_block():
    """Constructed, not captured: no capture carries both. The rule is the
    point. A face the device matched to a named person is not a stranger,
    whatever else the payload says, and an automation acting on "stranger"
    must never fire for someone it knows."""
    event = _event(
        [{"Person": _person("Mark", "2", "Family"), "Similarity": 100}],
        passerby={"GroupID": "1", "UID": "140"},
    )

    face = extract_face_data(event)

    assert face["recognized"] is True
    assert face["stranger"] is False
    assert face["passerby_id"] is None


def test_a_stop_is_not_a_result():
    """It carries the passerby block, so reading it would turn a recognised
    name into a stranger a few seconds later."""
    assert extract_face_data(STOP) is None


def test_another_code_is_not_a_face():
    event = dict(MARK, Code="VideoMotion")

    assert extract_face_data(event) is None


def test_the_closest_of_several_candidates_is_taken():
    event = _event(
        [
            {"Person": _person("Simon", "3", "Friends", uid="31"), "Similarity": 61},
            {"Person": _person("Mark", "2", "Family"), "Similarity": 94},
        ]
    )

    face = extract_face_data(event)

    assert face["name"] == "Mark"
    assert face["similarity"] == 94


def test_a_candidate_with_no_name_is_not_a_recognition():
    event = _event([{"Person": _person("  ", "2", "Family"), "Similarity": 99}])

    assert extract_face_data(event)["recognized"] is False


def test_the_doorbell_casing_and_a_string_payload_are_read():
    """DHIP sends `Action` and `Data`, and a payload can arrive as JSON text."""
    event = {
        "Code": "FaceRecognition",
        "Action": "Start",
        "Data": json.dumps(MARK["data"]),
    }

    assert extract_face_data(event)["name"] == "Mark"


def test_a_payload_that_is_not_json_is_not_a_face():
    event = {"Code": "FaceRecognition", "action": "Start", "data": "{not json"}

    assert extract_face_data(event) is None


# --- the coordinator ----------------------------------------------------------


class _Bus:
    def __init__(self):
        self.fired = []

    def fire(self, name, data):
        self.fired.append((name, data))


def _coordinator(channel=6):
    c = object.__new__(DahuaDataUpdateCoordinator)
    c._channel = channel
    c._address = "10.0.0.5"
    c.hass = SimpleNamespace(bus=_Bus())
    c.get_device_name = lambda: "Front"
    return c


def _faces(coordinator):
    return [
        data
        for name, data in coordinator.hass.bus.fired
        if name == EVENT_DAHUA_FACE_RECOGNITION
    ]


def test_a_recognised_face_goes_on_the_bus_and_becomes_the_last_face():
    c = _coordinator()
    heard = []
    c.add_face_listener(lambda: heard.append(True))

    c._handle_face_recognition(MARK)

    (fired,) = _faces(c)
    assert fired["recognized"] is True
    assert fired["name"] == "Mark"
    assert fired["device_name"] == "Front"
    assert fired["channel"] == 6
    assert c.get_last_face_data()["name"] == "Mark"
    assert heard == [True]


def test_a_stranger_goes_on_the_bus_and_leaves_the_last_name_standing():
    """An automation can act on a stranger; the sensor still answers who the
    last person recognised was."""
    c = _coordinator()
    c._handle_face_recognition(MARK)
    heard = []
    c.add_face_listener(lambda: heard.append(True))

    c._handle_face_recognition(STRANGER)

    assert _faces(c)[-1]["recognized"] is False
    assert _faces(c)[-1]["stranger"] is True
    assert c.get_last_face_data()["name"] == "Mark"
    assert heard == []


def test_a_stop_changes_nothing_and_fires_nothing():
    c = _coordinator()
    c._handle_face_recognition(MARK)

    c._handle_face_recognition(STOP)

    assert len(_faces(c)) == 1
    assert c.get_last_face_data()["name"] == "Mark"


def test_a_payload_the_parser_cannot_read_does_not_reach_the_stream(monkeypatch):
    """This runs for every event on the stream. A face payload shaped unlike
    the captures must cost this feature, not the stream."""
    from custom_components.dahua import dahua_utils

    def explode(_event):
        raise KeyError("a shape nobody captured")

    monkeypatch.setattr(dahua_utils, "extract_face_data", explode)
    c = _coordinator()

    c._handle_face_recognition(MARK)

    assert _faces(c) == []


def test_a_removed_listener_is_not_called_and_others_are():
    c = _coordinator()
    calls = []
    remove = c.add_face_listener(lambda: calls.append("first"))
    c.add_face_listener(lambda: calls.append("second"))

    remove()
    c._handle_face_recognition(MARK)

    assert calls == ["second"]


def test_one_coordinators_listeners_are_not_anothers():
    """The class default is shared by every coordinator built without
    __init__, which is why it is a tuple that is replaced, never a list that
    is appended to."""
    first, second = _coordinator(), _coordinator()
    calls = []
    first.add_face_listener(lambda: calls.append("first"))

    second._handle_face_recognition(MARK)

    assert calls == []


def test_the_last_face_handed_out_is_a_copy():
    c = _coordinator()
    c._handle_face_recognition(MARK)

    c.get_last_face_data()["name"] = "someone else"

    assert c.get_last_face_data()["name"] == "Mark"


# --- the sensor ---------------------------------------------------------------


@pytest.mark.parametrize(
    "events, created",
    [
        (["VideoMotion", "FaceRecognition"], True),
        (["All"], True),
        (["VideoMotion"], False),
        ([], False),
    ],
)
async def test_the_sensor_exists_only_where_faces_are_subscribed(events, created):
    """A sensor whose events are never asked for can only ever read unknown."""
    coordinator = _setup_coordinator(profile_support=False)
    coordinator.get_event_list = lambda: events
    coordinator.add_face_listener = lambda listener: (lambda: None)
    coordinator.get_last_face_data = lambda: {}
    hass, entry, added = _setup(coordinator)

    await sensor_module.async_setup_entry(hass, entry, adds_entities(added))

    assert (
        "DahuaFaceRecognitionSensor" in [type(s).__name__ for s in added]
    ) is created


def _sensor(face):
    entity = object.__new__(DahuaFaceRecognitionSensor)
    entity._coordinator = SimpleNamespace(
        get_last_face_data=lambda: dict(face),
        data={"id": "SERIAL1"},
    )
    entity.coordinator = entity._coordinator
    return entity


def test_the_sensor_shows_the_name_and_where_it_matched():
    c = _coordinator()
    c._handle_face_recognition(MARK)
    sensor = _sensor(c.get_last_face_data())

    assert sensor.native_value == "Mark"
    attributes = sensor.extra_state_attributes
    assert attributes["group_name"] == "Family"
    assert attributes["group_type"] == "BlackListDB"
    assert attributes["similarity"] == 100
    assert "updated_at" in attributes
    assert "name" not in attributes, "the name is the state, not an attribute"
    assert "stranger" not in attributes


def test_before_any_face_the_sensor_has_no_state():
    assert _sensor({}).native_value is None
