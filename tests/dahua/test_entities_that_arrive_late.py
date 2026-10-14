"""An answer that arrives late still gets to create its entities.

#1057. A capability probe that times out told us nothing, and the integration
wrote it down as "this device does not have that". #1043 measured the cost on a
nineteen channel recorder: two identical IP8M-2493E cameras, one with an
Infrared Mode select and a working Infrared light, the other with neither,
decided by which probe happened to expire. The set that failed was different on
every run, so it is contention rather than hardware, and the answer existed.

Reloading did not recover it, because a reload re-probes every channel at once
and hits the same wall. The entity set was decided at setup and nothing ever
asked again.

Three rules hold this together, and each has a test below:

* silence is not an answer, and is not remembered as one;
* a question that went unanswered is asked again later, when the device is idle
  and the same read is fast, but not for ever;
* an answer that arrives then creates the entities it gates, without a reload.
"""

import pytest

from homeassistant.core import HassJobType, get_hassjob_callable_job_type

from custom_components.dahua import DahuaDataUpdateCoordinator, late
from custom_components.dahua import coordinator as coordinator_module

SERIAL = "AMR044032F4F06B33E"


# --- silence is not an answer ------------------------------------------------


def _coordinator(refusals=None):
    coordinator = object.__new__(DahuaDataUpdateCoordinator)
    coordinator._probe_refusals = dict(refusals or {})
    return coordinator


def _silent(error="TimeoutError"):
    return {"answered": False, "status": None, "error": error}


def _refused(status=400):
    return {"answered": True, "status": status, "error": "ClientResponseError"}


def test_a_probe_that_timed_out_is_listed_as_unanswered():
    coordinator = _coordinator({"lighting": _silent()})

    assert coordinator.probes_that_did_not_answer() == {"lighting"}


def test_a_probe_the_device_refused_is_not():
    """A 400 is the device saying it does not serve that, which is an answer and
    the half worth keeping. Asking again would be asking it to repeat itself."""
    coordinator = _coordinator({"lighting": _refused()})

    assert coordinator.probes_that_did_not_answer() == set()


def test_a_device_that_has_refused_nothing_has_nothing_pending():
    assert _coordinator().probes_that_did_not_answer() == set()


def test_answering_clears_only_the_silence():
    coordinator = _coordinator({"lighting": _silent(), "ptz_position": _refused()})

    coordinator._note_probe_answered("lighting")
    coordinator._note_probe_answered("ptz_position")

    assert coordinator.probes_that_did_not_answer() == set()
    assert "ptz_position" in coordinator._probe_refusals, (
        "a refusal carrying a status is what this device will not do, and must "
        "survive another probe answering"
    )


# --- asked again, but not for ever -------------------------------------------


class _Retrying:
    """A coordinator with the retry machinery and nothing else."""

    def __init__(self, answers_on_attempt=None):
        self._probe_refusals = {"lighting": _silent()}
        self._capability_retries = {}
        self._supports_lighting = False
        self._address = "10.0.0.5"
        # A coordinator is per channel and the real one always has this. The
        # retry names it when it reports what a device would not answer, so a
        # recorder's nineteen channels are told apart in the log.
        self._channel = 14
        self.attempts = 0
        self._answers_on = answers_on_attempt
        self.signalled = []

    probes_that_did_not_answer = DahuaDataUpdateCoordinator.probes_that_did_not_answer
    _note_probe_answered = DahuaDataUpdateCoordinator._note_probe_answered
    _async_retry_unanswered_capabilities = (
        DahuaDataUpdateCoordinator._async_retry_unanswered_capabilities
    )

    async def _async_retry_lighting(self):
        self.attempts += 1
        if self._answers_on is not None and self.attempts >= self._answers_on:
            self._note_probe_answered("lighting")
            self._supports_lighting = True
            return True
        return False


@pytest.fixture
def _retryable_is_only_lighting(monkeypatch):
    """Narrow the registry to the one capability these two tests drive."""
    monkeypatch.setattr(
        coordinator_module,
        "RETRYABLE_CAPABILITIES",
        {"lighting": _Retrying._async_retry_lighting},
    )


async def test_a_silent_probe_is_asked_again(_retryable_is_only_lighting, monkeypatch):
    coordinator = _Retrying(answers_on_attempt=2)
    monkeypatch.setattr(late, "async_notify_capability_discovered", lambda *a: None)
    coordinator.hass = None
    coordinator.config_entry = type("E", (), {"entry_id": "e"})()
    coordinator.get_serial_number = lambda: SERIAL

    await coordinator._async_retry_unanswered_capabilities()

    assert coordinator.attempts == 1, "it should have asked once more"


async def test_it_stops_asking_after_the_cap(_retryable_is_only_lighting, monkeypatch):
    """A device that is genuinely mute must not cost a read every poll for the
    life of the entry. On the recorder in #1043 that would be twelve timeouts a
    cycle rather than twelve once."""
    monkeypatch.setattr(late, "async_notify_capability_discovered", lambda *a: None)
    coordinator = _Retrying(answers_on_attempt=None)
    coordinator.hass = None
    coordinator.config_entry = type("E", (), {"entry_id": "e"})()
    coordinator.get_serial_number = lambda: SERIAL

    for _ in range(10):
        await coordinator._async_retry_unanswered_capabilities()

    assert coordinator.attempts == coordinator_module.MAX_CAPABILITY_RETRIES


# --- and the entities turn up ------------------------------------------------


class _Entity:
    def __init__(self, unique_id):
        self.unique_id = unique_id


class _Coordinator:
    def __init__(self, serial, subentry_id):
        self._serial = serial
        self.subentry_id = subentry_id
        self.extras = []

    def get_serial_number(self):
        return self._serial


class _Entry:
    entry_id = "entry-1"

    def __init__(self, coordinators):
        self.runtime_data = {i: c for i, c in enumerate(coordinators)}
        self.unloads = []

    def async_on_unload(self, callback):
        self.unloads.append(callback)


class _Hass:
    def __init__(self):
        self.tasks = []

    def async_create_task(self, coro):
        self.tasks.append(coro)
        return coro

    async def run_pending(self):
        pending, self.tasks = self.tasks, []
        for coro in pending:
            await coro


@pytest.fixture
def _dispatcher(monkeypatch):
    """A dispatcher that records what connected and lets a test fire it."""
    connected = {}

    def connect(hass, signal, callback):
        # Calling the target directly is the one thing the real dispatcher does
        # not promise, and the difference cost #1043 every late entity it was
        # owed. Home Assistant decides where a target runs by inspecting it,
        # and hands anything that is neither a coroutine function nor a
        # `@callback` to a worker thread, where the rebuild cannot be started.
        # Asking Home Assistant's own classifier keeps that rule here rather
        # than a copy of it, and makes every test below a guard.
        assert (
            get_hassjob_callable_job_type(callback) is not HassJobType.Executor
        ), "the real dispatcher would run this on a worker thread"
        connected.setdefault(signal, []).append(callback)
        return lambda: connected[signal].remove(callback)

    def send(hass, signal, *args):
        for callback in list(connected.get(signal, [])):
            callback(*args)

    monkeypatch.setattr(late, "async_dispatcher_connect", connect)
    monkeypatch.setattr(late, "async_dispatcher_send", send)
    return connected


@pytest.fixture
def _two_channels():
    """Channel 0 and channel 15 of one recorder, as #1043 had them."""
    return [_Coordinator(SERIAL, "sub-0"), _Coordinator(SERIAL + "_15", "sub-15")]


async def _wire(hass, entry, channels, added):
    async def build(coordinator):
        return [
            _Entity(coordinator.get_serial_number() + suffix)
            for suffix in coordinator.extras
        ]

    def add(entities, config_subentry_id=None):
        added.extend((e.unique_id, config_subentry_id) for e in entities)

    await late.async_add_and_keep_watching(
        hass, entry, build, add, lambda e: e.runtime_data
    )


async def test_what_the_device_already_admits_to_is_added_at_setup(
    _dispatcher, _two_channels
):
    channels = _two_channels
    channels[0].extras = ["_light"]
    added = []
    await _wire(_Hass(), _Entry(channels), channels, added)

    assert added == [(SERIAL + "_light", "sub-0")]


async def test_a_late_answer_creates_the_entity_it_gates(_dispatcher, _two_channels):
    """The whole point. No reload, and the camera stops being the one with
    fewer entities than its twin."""
    channels = _two_channels
    hass, entry, added = _Hass(), _Entry(channels), []
    await _wire(hass, entry, channels, added)
    assert added == []

    channels[1].extras = ["_infrared_mode"]
    late.async_notify_capability_discovered(hass, entry.entry_id, SERIAL + "_15")
    await hass.run_pending()

    assert added == [(SERIAL + "_15_infrared_mode", "sub-15")]


async def test_the_entity_is_filed_under_the_channels_own_subentry(
    _dispatcher, _two_channels
):
    """A channel's entities belong with its channel (#827), whether they arrive
    on time or late."""
    channels = _two_channels
    hass, entry, added = _Hass(), _Entry(channels), []
    await _wire(hass, entry, channels, added)

    channels[1].extras = ["_light"]
    late.async_notify_capability_discovered(hass, entry.entry_id, SERIAL + "_15")
    await hass.run_pending()

    assert added[0][1] == "sub-15"


async def test_nothing_is_created_twice(_dispatcher, _two_channels):
    """`build` is called again on every discovery, so it returns entities that
    already exist. A second copy would collide on unique_id."""
    channels = _two_channels
    channels[1].extras = ["_light"]
    hass, entry, added = _Hass(), _Entry(channels), []
    await _wire(hass, entry, channels, added)
    assert len(added) == 1

    for _ in range(3):
        late.async_notify_capability_discovered(hass, entry.entry_id, SERIAL + "_15")
        await hass.run_pending()

    assert len(added) == 1, added


async def test_only_the_channel_that_answered_is_rebuilt(_dispatcher, _two_channels):
    """A platform's build may read from the device: select.py enumerates PTZ
    presets. One channel answering must not cost a read on the other eighteen."""
    channels = _two_channels
    hass, entry, added = _Hass(), _Entry(channels), []
    await _wire(hass, entry, channels, added)

    asked = []

    async def build(coordinator):
        asked.append(coordinator.get_serial_number())
        return []

    await late.async_add_and_keep_watching(
        hass, entry, build, lambda *a, **k: None, lambda e: e.runtime_data
    )
    asked.clear()

    late.async_notify_capability_discovered(hass, entry.entry_id, SERIAL + "_15")
    await hass.run_pending()

    assert asked == [SERIAL + "_15"]


async def test_the_listener_is_dropped_when_the_entry_unloads(
    _dispatcher, _two_channels
):
    """Otherwise a reload leaves the previous run's listener adding entities
    against coordinators that no longer exist."""
    entry = _Entry(_two_channels)
    await _wire(_Hass(), entry, _two_channels, [])

    assert entry.unloads, "nothing registered an unload"


async def test_a_late_answer_arrives_through_the_real_dispatcher(hass):
    """The same thing again, with nothing faked between here and the loop.

    Every test above replaces the dispatcher, so until #1043 was reported on a
    1.3.0 install none of this had ever run against the real one. It did not
    work: the listener was handed to a worker thread, `hass.async_create_task`
    refused to be called from there, and five channels that finally answered
    created nothing.

        RuntimeError: Detected that custom integration 'dahua' calls
        hass.async_create_task from a thread other than the event loop

    So one test uses the real `hass`, the real `async_dispatcher_connect` and
    the real `async_dispatcher_send`, and asserts the entity exists at the end.
    """
    channels = [_Coordinator(SERIAL, "sub-0"), _Coordinator(SERIAL + "_15", "sub-15")]
    entry, added = _Entry(channels), []

    async def build(coordinator):
        return [
            _Entity(coordinator.get_serial_number() + suffix)
            for suffix in coordinator.extras
        ]

    def add(entities, config_subentry_id=None):
        added.extend((e.unique_id, config_subentry_id) for e in entities)

    await late.async_add_and_keep_watching(
        hass, entry, build, add, lambda e: e.runtime_data
    )
    assert added == []

    channels[1].extras = ["_infrared_mode"]
    late.async_notify_capability_discovered(hass, entry.entry_id, SERIAL + "_15")
    await hass.async_block_till_done()

    assert added == [(SERIAL + "_15_infrared_mode", "sub-15")]
