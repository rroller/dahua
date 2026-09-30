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

from . import adds_entities


class _Coordinator:
    # The platforms file each channel's entities under its own subentry, so they
    # read this on every entity they add. None is a single camera, and is what
    # `async_add_entities` wants for an entry that has no subentries.
    subentry_id = None

    def __init__(self, doorbell=True, timestamp=0):
        self._doorbell = doorbell
        self._timestamp = timestamp
        self._channel = 0
        self._dahua_event_listeners = {}

    def is_doorbell(self):
        return self._doorbell

    def get_device_name(self):
        return "Front Door"

    def get_serial_number(self):
        return "SER1"

    def get_event_timestamp(self, name):
        return self._timestamp

    def event_is_momentary(self, name):
        return False

    # The real one. It returns the callback that undoes the subscription, which
    # the entity hands to `async_on_remove`, and a stand-in returning None would
    # be accepted in silence.
    add_dahua_event_listener = DahuaDataUpdateCoordinator.add_dahua_event_listener
    get_event_key = DahuaDataUpdateCoordinator.get_event_key


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
    coordinator = _Coordinator(doorbell=True)
    hass = type("H", (), {"data": {}})()
    await async_setup_entry(hass, type("E", (), {"entry_id": "e1",
                          "runtime_data": {0: coordinator}})(), adds_entities(added))

    assert len(added) == 1
    assert isinstance(added[0], DahuaDoorbellEvent)


async def test_a_camera_does_not():
    """There is no button, so the entity would sit at unknown for ever."""
    added = []
    coordinator = _Coordinator(doorbell=False)
    hass = type("H", (), {"data": {}})()
    await async_setup_entry(hass, type("E", (), {"entry_id": "e1",
                          "runtime_data": {0: coordinator}})(), adds_entities(added))

    assert added == []


# --- firing -------------------------------------------------------------------

async def test_it_fires_on_the_press():
    c = _Coordinator(timestamp=1_700_000_000)
    entity = _entity(c)

    entity._async_doorbell_pressed()

    assert entity.fired == ["ring"]


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
    assert entity.event_types == ["ring"], (
        "Home Assistant refuses to accept a doorbell that cannot ring"
    )


def test_a_doorbell_must_be_able_to_ring():
    """Home Assistant checks this itself, and only warns.

    On a real install the entity was created and then:

        Entity event.front_yard_cctv_front_door_bell_doorbell is a doorbell
        event entity but does not support the 'ring' event type. This will stop
        working in Home Assistant 2027.4

    The check in homeassistant/components/event is exactly:

        if (self.device_class == EventDeviceClass.DOORBELL
                and DoorbellEventType.RING not in self.event_types):

    A warning rather than an error, so nothing failed and the entity worked.
    It would simply have stopped in 2027.4.
    """
    entity = _entity(_Coordinator())

    assert "ring" in entity.event_types


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


# --- and the thing that lets one of them leave -------------------------------

def _coordinator_with_listeners():
    c = object.__new__(DahuaDataUpdateCoordinator)
    c._channel = 0
    c._dahua_event_listeners = {}
    return c


def test_removing_one_listener_leaves_the_other():
    """Two entities can want one event, so one going away must not take the
    other's subscription with it."""
    c = _coordinator_with_listeners()
    called = []
    drop_first = c.add_dahua_event_listener("DoorbellPressed",
                                            lambda: called.append("sensor"))
    c.add_dahua_event_listener("DoorbellPressed", lambda: called.append("event"))

    drop_first()

    for listener in c._dahua_event_listeners[c.get_event_key("DoorbellPressed")]:
        listener()
    assert called == ["event"]


def test_the_key_goes_when_its_last_listener_does():
    """Not tidiness. Two places read whether a key has listeners to decide
    behaviour: `_dispatch_event` skips a code nothing listens for, and
    `translate_event_code` decides whether to report CrossLineDetection alongside
    the SmartMotion translation. Both read the key, and an empty list is still a
    key, so leaving one behind answers yes on behalf of an entity that is gone."""
    c = _coordinator_with_listeners()
    drop = c.add_dahua_event_listener("DoorbellPressed", lambda: None)

    drop()

    assert c._dahua_event_listeners == {}


def test_removing_twice_is_harmless():
    """Home Assistant drains its own on-remove list, and an entity that is
    removed during setup can be removed again. Raising the second time would turn
    that into a traceback in the log."""
    c = _coordinator_with_listeners()
    drop = c.add_dahua_event_listener("DoorbellPressed", lambda: None)

    drop()
    drop()

    assert c._dahua_event_listeners == {}


def test_removing_twice_is_harmless_while_another_listener_remains():
    """The case the test above cannot see. With the key already gone the second
    call returns at the first line, so it passes whether or not the removal itself
    is careful. Leave a sibling behind and the key survives, so the second call
    reaches the list and a bare `list.remove` raises ValueError.

    Found by mutation: deleting the membership check left the test above green."""
    c = _coordinator_with_listeners()
    kept = lambda: None                                      # noqa: E731
    drop = c.add_dahua_event_listener("DoorbellPressed", lambda: None)
    c.add_dahua_event_listener("DoorbellPressed", kept)

    drop()
    drop()

    assert c._dahua_event_listeners[c.get_event_key("DoorbellPressed")] == [kept]


def test_a_plate_listener_can_be_dropped_too():
    """Same API, same omission: the authorized vehicle sensor had no way to stop
    being called either."""
    c = object.__new__(DahuaDataUpdateCoordinator)
    c._plate_listeners = []
    kept = lambda: None                                      # noqa: E731
    drop = c.add_plate_listener(lambda: None)
    c.add_plate_listener(kept)

    drop()

    assert c._plate_listeners == [kept]


# --- being added, and being removed ------------------------------------------
#
# Everything above drives the listener mechanics on the coordinator directly. The
# entity's own `async_added_to_hass` was never called, so what it does with the remover
# it is handed had no test -- which is the half that #842 was about.

async def test_it_subscribes_when_added_and_lets_go_when_removed():
    """The remover goes to `async_on_remove` rather than being discarded. Whether a key
    has listeners decides whether `_dispatch_event` reports that code at all, so a
    callback left behind answers "yes, something reads this" for an entity that is
    gone."""
    coordinator = _Coordinator()
    entity = _entity(coordinator)

    await entity.async_added_to_hass()

    key = coordinator.get_event_key("DoorbellPressed")
    assert key in coordinator._dahua_event_listeners, "never subscribed"

    assert entity._on_remove, "registered nothing to undo the subscription"
    for undo in list(entity._on_remove):
        undo()

    assert coordinator._dahua_event_listeners == {}, (
        "the callback outlived the entity")


def test_the_doorbell_event_is_pushed_not_polled():
    """It exists because an event arrived; there is nothing to poll for."""
    assert _entity(_Coordinator()).should_poll is False
