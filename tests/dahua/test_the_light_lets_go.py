"""When something other than Home Assistant changes the camera's white light.

Turning the illuminator on through this integration writes a CGI override and remembers
what the camera looked like first, so that turning it off again can put it back. That
snapshot is the problem: the camera can be changed from its own web UI, from the app, or
by a schedule, and a snapshot taken before any of that is no longer something anybody
wants restored. Writing it back would undo a change the user made deliberately.

So the entity watches for it. Every coordinator update compares the camera's current
WhiteLight mode and brightness against what HA believes it set, and a mismatch releases
ownership: the saved snapshot is discarded, `_manual_on` goes false, and **nothing is
written to the camera**. The new external configuration is left exactly as it is.

None of that was executed by the suite. The three functions it lives in held 91 of
`light.py`'s 123 uncovered lines, and every one of their failure modes is silent:

* A reboot makes the camera's state differ from HA's override for a moment, and that is
  not an external change. If reboot recovery did not suppress the comparison, every
  camera restart would release the override instead of restoring it.
* Two coordinator updates arrive faster than the release completes. Without the task
  guard that is two concurrent releases on one Store.
* The release re-checks after it gets CPU time, because the camera may have changed back.
* A Store failure must keep ownership rather than lose it, so a later update can retry.

The two halves are tested separately on purpose. These are background tasks on `hass`,
so the fake records them rather than running them: the update tests assert which task
was scheduled, and the release tests await the coroutine directly. Letting the fake run
them would make every scheduling test depend on the release's behaviour as well.
"""

import asyncio

import pytest
from unittest.mock import Mock

from custom_components.dahua.light import DahuaIlluminator

CHANNEL = 3
PROFILE = "1"
INDEX = 0
FIELD = "MiddleLight"
BASE = f"table.Lighting_V2[{CHANNEL}][{PROFILE}][{INDEX}]"

# What HA believes it set: brightness 255 goes on the wire as 100.
HA_BRIGHTNESS = 255
ON_THE_CAMERA = 100


class _Client:
    """Records anything written. The point of the release is that nothing is."""

    def __init__(self):
        self.writes = []
        self.scheme = "WhiteMode"

    async def async_set_lighting_v2_raw(self, *args, **kwargs):
        self.writes.append(("v2_raw", args, kwargs))

    async def async_set_lighting_v2(self, *args, **kwargs):
        self.writes.append(("v2", args, kwargs))

    async def async_set_lighting_scheme(self, *args, **kwargs):
        self.writes.append(("scheme", args, kwargs))
        return self.scheme


class _Coordinator:
    def __init__(self, mode="Manual", light=ON_THE_CAMERA, generation=0):
        self.client = _Client()
        self.data = {}
        if mode is not None:
            self.data[f"{BASE}.Mode"] = mode
            self.data[f"{BASE}.{FIELD}[0].Light"] = light
        self.generation = generation

    def get_camera_reboot_generation(self):
        return self.generation

    def get_serial_number(self):
        return "SERIAL1"


class _Store:
    def __init__(self, fails=False):
        self.removed = 0
        self.fails = fails

    async def async_remove(self):
        if self.fails:
            raise RuntimeError("the store is unwritable")
        self.removed += 1


class _Hass:
    """Records background tasks instead of running them."""

    def __init__(self):
        self.tasks = []

    def async_create_background_task(self, coro, name):
        self.tasks.append((name, coro))
        return _Task(coro)

    def names(self):
        return [name for name, _ in self.tasks]

    def close(self):
        """A coroutine that is never awaited warns; a test that only asserts the
        task was created has no reason to run it."""
        while self.tasks:
            _name, coro = self.tasks.pop(0)
            coro.close()


class _Task:
    def __init__(self, coro):
        self._coro = coro
        self._done = False

    def done(self):
        return self._done


def _light(
    coordinator,
    *,
    manual_on=True,
    store=None,
    hass=None,
    restore=(CHANNEL, PROFILE, INDEX, FIELD),
):
    """Only the attributes these three functions read, so nothing passes on state
    the test did not set. Built the way `test_light.py` builds one."""
    entity = object.__new__(DahuaIlluminator)
    entity._coordinator = coordinator
    entity.coordinator = coordinator
    entity.hass = hass or _Hass()
    entity.async_write_ha_state = Mock()
    entity._manual_on = manual_on
    entity._light_restore = restore
    entity._scheme_restore = None
    entity._last_brightness = HA_BRIGHTNESS
    entity._restore_store = store if store is not None else _Store()
    entity._seen_reboot_generation = coordinator.get_camera_reboot_generation()
    entity._reboot_recovery_task = None
    entity._external_change_task = None
    return entity


@pytest.fixture(autouse=True)
def _no_unawaited_coroutines():
    """Anything a test created and chose not to run is closed here rather than
    left to warn at interpreter exit, where it names no test."""
    created = []
    yield created
    for hass in created:
        hass.close()


# --- is the camera still showing what HA set? -------------------------------


def test_no_override_means_there_is_nothing_to_compare():
    """None, not False. An entity that never took ownership has not been taken
    over, and False here would schedule a release of nothing."""
    light = _light(_Coordinator(), restore=None)

    assert light._current_override_matches_coordinator_data() is None


def test_a_coordinator_with_no_mode_yet_is_not_an_external_change():
    """Also None. The poll may not have read Lighting_V2 yet, or the device may not
    serve it at all, and absence of evidence must not release the override."""
    light = _light(_Coordinator(mode=None))

    assert light._current_override_matches_coordinator_data() is None


def test_the_camera_showing_what_HA_set_matches():
    light = _light(_Coordinator(mode="Manual", light=ON_THE_CAMERA))

    assert light._current_override_matches_coordinator_data() is True


def test_a_mode_somebody_else_chose_does_not_match():
    """The common external change: the camera's own UI switches WhiteLight off
    schedule, or an app sets it to auto."""
    light = _light(_Coordinator(mode="Auto", light=ON_THE_CAMERA))

    assert light._current_override_matches_coordinator_data() is False


def test_a_brightness_somebody_else_chose_does_not_match():
    light = _light(_Coordinator(mode="Manual", light=25))

    assert light._current_override_matches_coordinator_data() is False


def test_an_unreadable_brightness_is_not_evidence_of_a_takeover():
    """True, deliberately. The mode is still Manual, so HA's override is intact;
    a brightness the device reported as something unparseable is a gap in what we
    can read, not a change somebody made."""
    light = _light(_Coordinator(mode="Manual", light="dim"))

    assert light._current_override_matches_coordinator_data() is True


# --- what a coordinator update decides to do --------------------------------


def test_an_update_with_no_override_active_schedules_nothing(_no_unawaited_coroutines):
    """The light is off as far as HA is concerned, so whatever the camera is doing
    is not HA's business."""
    hass = _Hass()
    _no_unawaited_coroutines.append(hass)
    light = _light(_Coordinator(mode="Auto"), manual_on=False, hass=hass)

    light._handle_coordinator_update()

    assert hass.names() == []


def test_an_update_that_still_matches_schedules_nothing(_no_unawaited_coroutines):
    hass = _Hass()
    _no_unawaited_coroutines.append(hass)
    light = _light(_Coordinator(mode="Manual"), hass=hass)

    light._handle_coordinator_update()

    assert hass.names() == []


def test_an_external_change_schedules_the_release(_no_unawaited_coroutines):
    hass = _Hass()
    _no_unawaited_coroutines.append(hass)
    light = _light(_Coordinator(mode="Auto"), hass=hass)

    light._handle_coordinator_update()

    assert len(hass.names()) == 1
    assert "external_change" in hass.names()[0]


def test_a_second_update_does_not_start_a_second_release(_no_unawaited_coroutines):
    """Coordinator updates arrive per poll and the release awaits a Store. Two
    concurrent releases would both remove the same snapshot and both write state."""
    hass = _Hass()
    _no_unawaited_coroutines.append(hass)
    light = _light(_Coordinator(mode="Auto"), hass=hass)

    light._handle_coordinator_update()
    light._handle_coordinator_update()

    assert len(hass.names()) == 1


def test_the_state_is_still_written_when_nothing_was_scheduled():
    """The update is still an update. Skipping the base class's write would leave
    the entity showing whatever it showed before the poll."""
    light = _light(_Coordinator(mode="Manual"))

    light._handle_coordinator_update()

    assert light.async_write_ha_state.called


# --- a reboot is not an external change -------------------------------------


def test_a_reboot_with_no_override_just_moves_the_baseline(_no_unawaited_coroutines):
    """Nothing to recover, so the new generation is simply accepted. Leaving the
    baseline behind would make the next poll look like another reboot."""
    hass = _Hass()
    _no_unawaited_coroutines.append(hass)
    coordinator = _Coordinator(mode="Auto", generation=0)
    light = _light(coordinator, manual_on=False, hass=hass)
    coordinator.generation = 1

    light._handle_coordinator_update()

    assert light._seen_reboot_generation == 1
    assert hass.names() == []


def test_a_reboot_with_an_override_starts_recovery(_no_unawaited_coroutines):
    hass = _Hass()
    _no_unawaited_coroutines.append(hass)
    coordinator = _Coordinator(mode="Manual", generation=0)
    light = _light(coordinator, hass=hass)
    coordinator.generation = 1

    light._handle_coordinator_update()

    assert len(hass.names()) == 1
    assert "reboot_recovery" in hass.names()[0]


def test_a_reboot_is_not_mistaken_for_somebody_changing_the_light(
    _no_unawaited_coroutines,
):
    """The one that matters most. A camera coming back up reports a WhiteLight
    state that does not match HA's override, which is exactly what an external
    change looks like. If the comparison ran anyway, every reboot would discard the
    snapshot that reboot recovery exists to replay, so the light would stay off
    after every restart and HA would report it as off rather than fixing it.
    """
    hass = _Hass()
    _no_unawaited_coroutines.append(hass)
    coordinator = _Coordinator(mode="Auto", generation=0)
    light = _light(coordinator, hass=hass)
    coordinator.generation = 1

    light._handle_coordinator_update()

    assert len(hass.names()) == 1
    assert "reboot_recovery" in hass.names()[0]
    assert not any("external_change" in name for name in hass.names())


def test_the_baseline_is_not_moved_until_recovery_owns_it(_no_unawaited_coroutines):
    """Recovery is handed the generation it is recovering to, and moves the
    baseline itself. Moving it here would make a recovery that failed look done."""
    hass = _Hass()
    _no_unawaited_coroutines.append(hass)
    coordinator = _Coordinator(mode="Manual", generation=0)
    light = _light(coordinator, hass=hass)
    coordinator.generation = 4

    light._handle_coordinator_update()

    assert light._seen_reboot_generation == 0


# --- releasing it -----------------------------------------------------------


async def test_the_release_discards_the_snapshot_and_lets_go():
    store = _Store()
    light = _light(_Coordinator(mode="Auto"), store=store)

    await light._async_release_externally_changed_override()

    assert store.removed == 1
    assert light._manual_on is False
    assert light.is_on is False
    assert light._light_restore is None


async def test_the_release_writes_nothing_to_the_camera():
    """The whole point. The saved snapshot was taken before somebody made a change
    on purpose, so restoring it would undo their change; the new configuration is
    respected exactly as it is."""
    coordinator = _Coordinator(mode="Auto")
    light = _light(coordinator)

    await light._async_release_externally_changed_override()

    assert coordinator.client.writes == []


async def test_the_release_reports_the_new_state():
    light = _light(_Coordinator(mode="Auto"))

    await light._async_release_externally_changed_override()

    assert light.async_write_ha_state.called


async def test_a_release_for_a_light_that_is_already_off_does_nothing():
    """It was scheduled, then something else released first."""
    store = _Store()
    light = _light(_Coordinator(mode="Auto"), manual_on=False, store=store)

    await light._async_release_externally_changed_override()

    assert store.removed == 0
    assert light.async_write_ha_state.called is False


async def test_a_camera_that_changed_back_keeps_the_override():
    """Re-checked after the task gets CPU time, because the decision was made one
    poll ago. A camera that has returned to what HA set has not taken anything
    over, and releasing here would lose the snapshot for no reason."""
    store = _Store()
    light = _light(_Coordinator(mode="Manual"), store=store)

    await light._async_release_externally_changed_override()

    assert store.removed == 0
    assert light._manual_on is True
    assert light._light_restore is not None


async def test_a_store_that_will_not_clear_keeps_ownership():
    """Ownership is in-memory state that says a snapshot exists on disk. Dropping
    it while the snapshot survives would orphan the file, and the next restart
    would replay a stale override nobody can cancel. Keeping it lets a later
    coordinator update retry.
    """
    store = _Store(fails=True)
    light = _light(_Coordinator(mode="Auto"), store=store)

    await light._async_release_externally_changed_override()

    assert light._manual_on is True, "ownership was dropped with the snapshot intact"
    assert light._light_restore is not None


async def test_the_task_handle_is_cleared_either_way():
    """It is what the next update checks before scheduling another. A handle left
    behind by a failed release would block every retry for the life of the entity.
    """
    light = _light(_Coordinator(mode="Auto"), store=_Store(fails=True))
    light._external_change_task = object()

    await light._async_release_externally_changed_override()

    assert light._external_change_task is None


async def test_cancellation_is_not_swallowed_as_a_store_failure():
    """The broad handler beside it exists so a Store failure keeps ownership.
    Cancellation is Home Assistant shutting the task down, and a background task
    that ignores it stops Home Assistant from stopping.
    """

    class _Cancels(_Store):
        async def async_remove(self):
            raise asyncio.CancelledError

    light = _light(_Coordinator(mode="Auto"), store=_Cancels())

    with pytest.raises(asyncio.CancelledError):
        await light._async_release_externally_changed_override()

    assert light._external_change_task is None, "the finally block was skipped"
