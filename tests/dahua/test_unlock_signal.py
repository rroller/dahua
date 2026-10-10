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


@pytest.mark.parametrize(
    "event",
    [
        {"Code": "BackKeyLight", "Data": {}},
        {"Code": "BackKeyLight", "Data": {"State": None}},
        {"Code": "BackKeyLight", "Data": {"State": "nonsense"}},
        {"Code": "BackKeyLight", "Data": "not a dict"},
        {"Code": "BackKeyLight"},
    ],
)
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
        {"Code": "PhoneCallDetect", "Action": "Pulse", "Data": {}}
    )

    assert codes == ["DoorbellPressed"]


def test_the_mapping_is_what_the_comment_says():
    assert DOORBELL_STATE_EVENTS == {8: "DoorUnlocked", 9: "DoorUnlockFailed"}


# --- and it can now actually raise something --------------------------------
#
# translate_event_code emitted DoorUnlocked, and _dispatch_event then threw it
# away. The Pulse branch excluded both DOORBELL_STATE_EVENTS values from the
# momentary path and sent them to the call-state check instead, where
# `pressed = numeric_state in {1, 2}` is False for State 8 and the timestamp is
# written to 0. So the code survived translation, as the tests above pin, and
# was structurally incapable of raising anything that listened for it -- which
# is the only reason it is derived at all. _note_unknown_doorbell_state does not
# complain either, because 8 is a state it knows.
#
# They are moments, not call states: the door unlocked, and no Stop is coming.
# So the momentary branch is theirs, with the same hold the doorbell press got
# in #761, and PULSE_STATE_CODES alone decides who goes to the call-state check.


def _dispatching_vto():
    c = _vto()
    c._dahua_event_timestamp = {}
    c.hass = SimpleNamespace(
        bus=SimpleNamespace(fire=lambda *a, **k: None),
        async_create_task=lambda coro: coro.close(),
    )
    c.get_device_name = lambda: "Front Door"
    c._handle_anpr_plate = lambda event: None
    c.get_ivs_rules = lambda: []
    return c


def _listen(c, code):
    key = c.get_event_key(code)
    c.add_dahua_event_listener(code, lambda: None)
    return key


def test_an_unlock_reaches_a_listener():
    c = _dispatching_vto()
    key = _listen(c, "DoorUnlocked")

    c.on_receive_vto_event(_event(8))

    assert c._dahua_event_timestamp[key] > 0, "the unlock could not raise anything"


def test_a_failed_unlock_reaches_its_own_listener():
    c = _dispatching_vto()
    key = _listen(c, "DoorUnlockFailed")

    c.on_receive_vto_event(_event(9))

    assert c._dahua_event_timestamp[key] > 0


def test_an_unlock_is_momentary_so_it_clears_itself():
    """There is no Stop for an unlock, so whatever listens has to be able to
    come down again on its own, the way the press does."""
    c = _dispatching_vto()
    _listen(c, "DoorUnlocked")

    c.on_receive_vto_event(_event(8))

    assert c.event_is_momentary("DoorUnlocked") is True


def test_the_press_still_goes_to_the_call_state_check():
    """The guard. DoorbellPressed does carry a state, and it must keep being
    judged on it rather than becoming a momentary that any state raises."""
    c = _dispatching_vto()
    key = _listen(c, "DoorbellPressed")

    c.on_receive_vto_event(_event(1))
    assert c._dahua_event_timestamp[key] > 0, "a ring stopped raising the sensor"

    c.on_receive_vto_event(_event(6))
    assert c._dahua_event_timestamp[key] == 0, "state 6 is not a ring"
    assert c.event_is_momentary("DoorbellPressed") is False


def test_an_unlock_still_clears_the_press():
    """State 8 is not a ring, so the button sensor comes down. That is the
    behaviour the unlock codes were added beside, not instead of."""
    c = _dispatching_vto()
    press = _listen(c, "DoorbellPressed")
    _listen(c, "DoorUnlocked")

    c.on_receive_vto_event(_event(1))
    assert c._dahua_event_timestamp[press] > 0

    c.on_receive_vto_event(_event(8))

    assert c._dahua_event_timestamp[press] == 0
