"""A VTO's unlock is reported, and was being discarded on arrival.

BackKeyLight carries the VTO's call state, and ringing is only part of what it
reports. Every one of them was collapsed to DoorbellPressed:

    if code == "BackKeyLight" or code == "PhoneCallDetect":
        return ["DoorbellPressed"]

so State 8 (Unlock) was read only as "not a ring", which silently cleared the
button sensor, and nothing downstream could see that the door had opened.

Measured on a VTO2000A, pressing the integration's own Open Door button:

    19:42:13  DEBUG  Opening door on 192.168.0.232
    19:42:14  DEBUG  VTO Data received: {'Action': 'Pulse', 'Code': 'BackKeyLight',
                     'Data': {'State': 8, ...}, 'Index': -1}

0.7 seconds, and **no AccessControl event**, so this is the only signal an
unlock can be confirmed from. myhomeiot/DahuaVTO documents 9 as the failed
counterpart and its reference lock triggers on exactly these.
"""

from types import SimpleNamespace

import pytest

from custom_components.dahua import (
    DOORBELL_STATE_EVENTS,
    DahuaDataUpdateCoordinator,
    doorbell_state,
)


def _vto():
    c = object.__new__(DahuaDataUpdateCoordinator)
    c._channel = 0
    c._address = "10.0.0.5"
    c._dahua_event_listeners = {}
    return c


def _event(state):
    return {"Code": "BackKeyLight", "Action": "Pulse", "Data": {"State": state}}


# --- reading the state ------------------------------------------------------

def test_the_measured_unlock():
    assert doorbell_state(_event(8)) == 8


@pytest.mark.parametrize("value,expected", [(8, 8), ("8", 8), (0, 0), (2, 2)])
def test_the_state_is_read_as_a_number(value, expected):
    assert doorbell_state(_event(value)) == expected


@pytest.mark.parametrize("event", [
    {"Code": "BackKeyLight", "Data": {}},
    {"Code": "BackKeyLight", "Data": {"State": None}},
    {"Code": "BackKeyLight", "Data": {"State": "nonsense"}},
    {"Code": "BackKeyLight", "Data": "not a dict"},
    {"Code": "BackKeyLight"},
])
def test_a_state_it_did_not_report(event):
    assert doorbell_state(event) is None


# --- what survives translation ----------------------------------------------

def test_the_unlock_now_survives():
    """The whole point: something downstream can see the door opened."""
    codes = _vto().translate_event_code(_event(8))

    assert "DoorUnlocked" in codes


def test_a_failed_unlock_is_distinguishable():
    codes = _vto().translate_event_code(_event(9))

    assert "DoorUnlockFailed" in codes
    assert "DoorUnlocked" not in codes


def test_the_doorbell_event_is_still_produced():
    """Additive: nothing that used to be dispatched stops being dispatched."""
    for state in (0, 1, 2, 8, 9, 11):
        assert "DoorbellPressed" in _vto().translate_event_code(_event(state))


@pytest.mark.parametrize("state", [0, 1, 2, 4, 5, 6, 7, 11])
def test_states_that_are_not_about_the_lock_add_nothing(state):
    """Ringing, answering and rebooting must not look like a door opening."""
    codes = _vto().translate_event_code(_event(state))

    assert codes == ["DoorbellPressed"]


def test_an_amcrest_doorbell_is_unaffected():
    """PhoneCallDetect carries no State and must keep behaving as before."""
    codes = _vto().translate_event_code(
        {"Code": "PhoneCallDetect", "Action": "Pulse", "Data": {}})

    assert codes == ["DoorbellPressed"]


def test_the_mapping_is_what_the_comment_says():
    assert DOORBELL_STATE_EVENTS == {8: "DoorUnlocked", 9: "DoorUnlockFailed"}
