"""One button press must produce one ring, however many states report it.

#944: the Doorbell event entity fired twice for a single press while the Button
Pressed binary sensor showed one. Three things narrowed it, all from
@felipecrs's DHI-VTO2202F-P-S2:

**The bus capture.** One press produced two BackKeyLight pulses 96 ms apart:

    time_fired 02:40:36.637675   Code=BackKeyLight  Data={"State": 1}
    time_fired 02:40:36.733860   Code=BackKeyLight  Data={"State": 2}

Distinct context ids, so two fires rather than one event listed twice.

**The entity history.** Two Ring rows at 11:40:36 PM, one press.

**The diagnostics.** `listener_keys` reads `{"DoorbellPressed-0": 2}`, which is
the healthy number: the binary sensor and this entity. So it is not a
subscription leaking across reloads, which was the leading theory and the thing
the counts were added to answer (#1102).

Both states are in `DOORBELL_RINGING_STATES` deliberately -- some devices send
only 1 and some only 2 -- so each one stamps the timestamp and runs the
listeners. The binary sensor is level shaped and sees on, then on. The event
entity is edge shaped and had no edge to read, so it rang twice.

These tests drive the real `_dispatch_event`, because the fault is in the
sequence of calls it makes and a fake coordinator holding one fixed timestamp
cannot produce a sequence at all.
"""

from types import SimpleNamespace

import pytest

from custom_components.dahua import DahuaDataUpdateCoordinator
from custom_components.dahua import coordinator as coordinator_module
from custom_components.dahua import event as event_module
from custom_components.dahua.event import (
    DOORBELL_PRESSED,
    SAME_PRESS_SECONDS,
    DahuaDoorbellEvent,
)

# The UTC second of the press in @felipecrs's capture.
PRESSED_AT = 1_792_550_436


class _Clock:
    """Only `time.time` is read while an event is dispatched, so this stands in
    for the module's `time` for the duration of one test and lets a press be
    placed at a chosen second."""

    def __init__(self, now=PRESSED_AT):
        self.now = now

    def time(self):
        return self.now


@pytest.fixture
def clock(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(coordinator_module, "time", clock)
    return clock


@pytest.fixture(autouse=True)
def _skip_ha_plumbing(monkeypatch):
    monkeypatch.setattr(
        event_module.DahuaEventDrivenEntity, "__init__", lambda self, c, e: None
    )


def _coordinator():
    """A real coordinator, by the pattern the rest of the suite uses: the code
    under test is its own dispatch, so only what that path reads is supplied."""
    c = object.__new__(DahuaDataUpdateCoordinator)
    c._channel = 0
    c._address = "10.0.0.7"
    c._dahua_event_timestamp = {}
    c._dahua_event_listeners = {}
    c._alarm_inputs = []
    c.hass = SimpleNamespace(
        bus=SimpleNamespace(fire=lambda *a, **k: None),
        async_create_task=lambda coro: coro.close(),
    )
    c.get_device_name = lambda: "Front Door"
    c.get_ivs_rules = lambda: []
    c._handle_anpr_plate = lambda event: None
    return c


def _entity(coordinator):
    entity = object.__new__(DahuaDoorbellEvent)
    entity._coordinator = coordinator
    entity.rings = []
    entity._trigger_event = lambda t, attrs=None: entity.rings.append(t)
    entity.async_write_ha_state = lambda: None
    coordinator.add_dahua_event_listener(
        DOORBELL_PRESSED, entity._async_doorbell_pressed
    )
    return entity


def _back_key_light(coordinator, state):
    """One BackKeyLight pulse, in the casing the VTO's DHIP listener delivers.

    `Index: -1` is what this device really puts on these, and is the value
    `door_index` exists to refuse to read as a door.
    """
    coordinator._dispatch_event(
        {
            "Code": "BackKeyLight",
            "Action": "Pulse",
            "Index": -1,
            "Data": {"State": state, "UTC": 1_792_550_436},
        },
        "Pulse",
    )


# --- the reported fault -------------------------------------------------------


def test_the_captured_press_rings_once(clock):
    """@felipecrs's sequence, exactly. Two rings here is the bug."""
    c = _coordinator()
    entity = _entity(c)

    _back_key_light(c, 1)
    _back_key_light(c, 2)

    assert entity.rings == ["ring"]


def test_it_rings_once_when_the_two_states_straddle_a_second(clock):
    """96 ms apart lands in one second most of the time and in two sometimes.

    This is why the guard cannot be "the timestamp is the one I already rang
    on": the coordinator stores `int(time.time())`, so the same press reads as
    two different timestamps about one time in ten.
    """
    c = _coordinator()
    entity = _entity(c)

    _back_key_light(c, 1)
    clock.now += 1
    _back_key_light(c, 2)

    assert entity.rings == ["ring"]


def test_the_binary_sensor_still_sees_the_press(clock):
    """The negative control for the fix as a whole. Suppressing the second ring
    must not touch the timestamp, which is the only thing the Button Pressed
    sensor reads and the reason that sensor was always right about this."""
    c = _coordinator()
    _entity(c)

    _back_key_light(c, 1)
    _back_key_light(c, 2)

    assert c.get_event_timestamp(DOORBELL_PRESSED) == PRESSED_AT


# --- and a second caller is still a second ring -------------------------------


def test_a_second_press_after_the_call_ends_rings_again(clock):
    """The whole sequence of one call: ring, ring, idle. Then somebody else."""
    c = _coordinator()
    entity = _entity(c)

    _back_key_light(c, 1)
    _back_key_light(c, 2)
    clock.now += 29
    _back_key_light(c, 0)
    clock.now += 1
    _back_key_light(c, 1)

    assert entity.rings == ["ring", "ring"]


def test_the_release_rearms_immediately(clock):
    """Inside the window, so only the release can explain the second ring. A
    guard that waited for the window to expire would swallow this."""
    c = _coordinator()
    entity = _entity(c)

    _back_key_light(c, 1)
    _back_key_light(c, 0)
    _back_key_light(c, 1)

    assert entity.rings == ["ring", "ring"]


def test_a_device_that_never_says_it_stopped_still_rings_again(clock):
    """The other half, and the reason the release alone is not enough. Some
    doorbells report no idle state at all, which is why the binary sensor holds
    a press on a timer rather than waiting to be told it ended."""
    c = _coordinator()
    entity = _entity(c)

    _back_key_light(c, 1)
    clock.now += SAME_PRESS_SECONDS
    _back_key_light(c, 1)

    assert entity.rings == ["ring", "ring"]


def test_a_doorbell_that_only_reports_state_2_still_rings(clock):
    """State 2 is in the ringing set because devices exist that send only it,
    and #872's VTO2311R-WP is the report that put it there. A fix for the
    duplicate must not quietly become a fix that drops one of the two."""
    c = _coordinator()
    entity = _entity(c)

    _back_key_light(c, 2)

    assert entity.rings == ["ring"]


def test_the_release_on_its_own_rings_nothing(clock):
    """The behaviour that was already right, kept. The listener runs for the end
    of a call as well, and an event entity has no way to report an ending."""
    c = _coordinator()
    entity = _entity(c)

    _back_key_light(c, 0)

    assert entity.rings == []


# --- the window is the binary sensor's, not a second opinion ------------------


def test_the_window_is_the_one_the_button_sensor_holds():
    """Derived rather than copied. Two numbers for "this is still the same
    press" would drift apart, and the pair would disagree about a press that
    landed between them."""
    from custom_components.dahua.binary_sensor import MOMENTARY_EVENT_HOLD_SECONDS

    assert SAME_PRESS_SECONDS == MOMENTARY_EVENT_HOLD_SECONDS["DoorbellPressed"]
