"""A doorbell that reports BackKeyLight State 2 is still ringing.

The VTO reports its call state in the `State` field of a BackKeyLight event,
and more than one value means ringing. Only `1` was accepted, so a device
using `2` raised the Button Pressed sensor never -- the event reached the
Home Assistant bus, and the sensor stayed off.

myhomeiot/DahuaVTO documents the wider set:

    0      no call            5   answered from the VTH
    1, 2   Call/Ring          6   not answered
    4      voice message      8   unlock
                              11  rebooted

and its reference automation treats `State | int in [1, 2]` as the ring. That
project also warns the values vary by model, so this widens what counts as a
ring rather than claiming the mapping is complete -- the states below that must
NOT ring are the ones it names for something else entirely.
"""

from types import SimpleNamespace

import pytest

from custom_components.dahua import DOORBELL_RINGING_STATES, DahuaDataUpdateCoordinator


def _vto():
    c = object.__new__(DahuaDataUpdateCoordinator)
    c._channel = 0
    c._address = "10.0.0.5"
    c._dahua_event_timestamp = {}
    c._dahua_event_listeners = {}
    c.fired = []
    c.hass = SimpleNamespace(
        bus=SimpleNamespace(fire=lambda *a, **k: None),
        async_create_task=lambda coro: None,
    )
    c.get_device_name = lambda: "Front Door"
    c._handle_anpr_plate = lambda event: None
    return c


def _listening(c):
    """Register the way the integration does, because listeners are a list.

    #726 added a second entity for DoorbellPressed, so registering appends
    rather than assigns. A fixture that assigns puts a bare callable where
    dispatch expects a list, and every test here dies with
    "TypeError: 'function' object is not iterable".
    """
    key = c.get_event_key("DoorbellPressed")
    c.add_dahua_event_listener("DoorbellPressed", lambda: c.fired.append(key))
    return key


def _press(c, state):
    c.on_receive_vto_event({"Code": "BackKeyLight", "Action": "Pulse",
                            "Data": {"State": state}})


# --- ringing ----------------------------------------------------------------

@pytest.mark.parametrize("state", [1, 2])
def test_both_ringing_states_raise_the_sensor(state):
    c = _vto()
    key = _listening(c)

    _press(c, state)

    assert c._dahua_event_timestamp[key] > 0, "State %s is a ring" % state
    assert c.fired == [key]


def test_the_value_may_arrive_as_a_string():
    """The DHIP payload is JSON, but nothing guarantees the type."""
    c = _vto()
    key = _listening(c)

    _press(c, "2")

    assert c._dahua_event_timestamp[key] > 0


# --- not ringing ------------------------------------------------------------

@pytest.mark.parametrize("state", [0, 4, 5, 6, 7, 8, 9, 11])
def test_the_other_documented_states_do_not_ring(state):
    """Unlock and reboot must not look like somebody at the door."""
    c = _vto()
    key = _listening(c)

    _press(c, state)

    assert c._dahua_event_timestamp[key] == 0
    assert c.fired == [key], "the entity is still told to re-read"


def test_an_unreadable_state_does_not_ring():
    c = _vto()
    key = _listening(c)

    _press(c, None)
    assert c._dahua_event_timestamp[key] == 0

    _press(c, "not a number")
    assert c._dahua_event_timestamp[key] == 0


def test_the_ringing_set_is_what_the_comment_says():
    assert DOORBELL_RINGING_STATES == {1, 2}
