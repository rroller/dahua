"""The doorbell press, as Home Assistant's own doorbell primitive.

#715: `event` with the `doorbell` device class is what Home Assistant offers
for a doorbell, and what cards built on it expect. The Button Pressed binary
sensor stays: it holds a state for a few seconds, which is what an automation
waiting on `on` needs, while this is momentary, which is what something showing
"somebody rang at 19:42" needs.

Both want DoorbellPressed, and until now a second subscriber silently replaced
the first:

    self._dahua_event_listeners[event_key] = listener

Only the binary sensor ever subscribed, one per code, so nothing had collided
yet. Adding a second entity for the same event is what makes it matter.
"""

import pytest

from custom_components.dahua import DahuaDataUpdateCoordinator
from custom_components.dahua import event as event_module
from custom_components.dahua.const import EVENT, PLATFORMS
from custom_components.dahua.event import DahuaDoorbellEvent, async_setup_entry


class _Coordinator:
    def __init__(self, doorbell=True, timestamp=0):
        self._doorbell = doorbell
        self._timestamp = timestamp
        self.listeners = {}

    def is_doorbell(self):
        return self._doorbell

    def get_device_name(self):
        return "Front Door"

    def get_serial_number(self):
        return "SER1"

    def get_event_timestamp(self, name):
        return self._timestamp

    def add_dahua_event_listener(self, name, listener):
        self.listeners.setdefault(name, []).append(listener)


@pytest.fixture(autouse=True)
def _skip_ha_plumbing(monkeypatch):
    monkeypatch.setattr(event_module.DahuaEventDrivenEntity, "__init__",
                        lambda self, c, e: None)


def _entity(coordinator):
    entity = object.__new__(DahuaDoorbellEvent)
    entity._coordinator = coordinator
    entity.fired = []
    entity._trigger_event = lambda t, attrs=None: entity.fired.append(t)
    entity.async_write_ha_state = lambda: None
    return entity


# --- the platform ------------------------------------------------------------

def test_the_event_platform_is_registered():
    assert EVENT in PLATFORMS


async def test_a_doorbell_gets_one():
    added = []
    hass = type("H", (), {"data": {"dahua": {"e1": _Coordinator(doorbell=True)}}})()
    await async_setup_entry(hass, type("E", (), {"entry_id": "e1"})(), added.extend)

    assert len(added) == 1
    assert isinstance(added[0], DahuaDoorbellEvent)


async def test_a_camera_does_not():
    """There is no button, so the entity would sit at unknown for ever."""
    added = []
    hass = type("H", (), {"data": {"dahua": {"e1": _Coordinator(doorbell=False)}}})()
    await async_setup_entry(hass, type("E", (), {"entry_id": "e1"})(), added.extend)

    assert added == []


# --- firing -------------------------------------------------------------------

async def test_it_fires_on_the_press():
    c = _Coordinator(timestamp=1_700_000_000)
    entity = _entity(c)

    entity._async_doorbell_pressed()

    assert entity.fired == ["pressed"]


async def test_it_does_not_fire_on_the_release():
    """The listener runs for the start and the end; only one is a press."""
    c = _Coordinator(timestamp=0)
    entity = _entity(c)

    entity._async_doorbell_pressed()

    assert entity.fired == [], "a release was reported as a second press"


def test_it_declares_itself_a_doorbell():
    """Read these off an instance, never off the class.

    Home Assistant builds entity classes through a metaclass that rewrites
    every `_attr_x` in the class body into a property descriptor, so
    `DahuaDoorbellEvent._attr_device_class` is that descriptor rather than the
    value. The device class and the event types are what a dashboard card
    reads, so assert those.
    """
    entity = _entity(_Coordinator())

    assert entity.device_class == "doorbell"
    assert entity.event_types == ["pressed"]


def test_its_unique_id_does_not_collide_with_the_binary_sensor():
    entity = _entity(_Coordinator())

    assert entity.unique_id == "SER1_doorbell_event"


# --- the thing that makes two subscribers possible ---------------------------

def test_two_entities_can_want_the_same_event():
    """Assignment meant the second replaced the first, silently."""
    c = object.__new__(DahuaDataUpdateCoordinator)
    c._channel = 0
    c._dahua_event_listeners = {}
    called = []

    c.add_dahua_event_listener("DoorbellPressed", lambda: called.append("sensor"))
    c.add_dahua_event_listener("DoorbellPressed", lambda: called.append("event"))

    for listener in c._dahua_event_listeners[c.get_event_key("DoorbellPressed")]:
        listener()

    assert called == ["sensor", "event"]


def test_listeners_for_different_events_stay_separate():
    c = object.__new__(DahuaDataUpdateCoordinator)
    c._channel = 0
    c._dahua_event_listeners = {}

    c.add_dahua_event_listener("DoorbellPressed", lambda: None)
    c.add_dahua_event_listener("VideoMotion", lambda: None)

    assert len(c._dahua_event_listeners) == 2
