"""Which unanswered probes are worth waiting for, and which are simply absent.

Reported by @kmich on #1114, with the diagnosis already done: the
leftover-rows card could not appear on **any of his seventeen entries**, on a
VTH2421F-P, on IPCs and on an NVR alike. Every one reported

    cloud_upgrade: {"answered": false, "status": null, "error": "ValueError"}
    capability_retries: {"cloud_upgrade": 3}

while three of his monitors really did have leftover `disarming` and
`event_notifications` rows to offer.

**Two separate faults, and either one alone is enough to hold the card shut.**

1. ⭐ *A reply with no table was being filed as silence.* The device answered
   `configManager.getConfig` perfectly well; the reply just had no
   `_DHCloudUpgrade_` table in it, because this firmware has none.
   `get_cloud_upgrade_info` raised a bare `ValueError`, and
   `_note_probe_refusal` decides "did the device answer?" by looking for an
   HTTP `status` or an RPC2 `code`. A bare `ValueError` has neither. So the
   one shape that is *most certainly* an answer was the one recorded as no
   answer at all. `Rpc2TableAbsent` says so explicitly.

2. ⭐ *A probe that had given up still held the gate.* `_entry_is_wholly_healthy`
   waited on anything in `probes_that_did_not_answer`, whose justification is
   that "the retry builds the entity when the device finally replies". That
   stops being true once `_async_retry_capabilities` has spent
   `MAX_CAPABILITY_RETRIES`, and it was never true for a probe outside
   `RETRYABLE_CAPABILITIES`, which is never re-asked at all. After that the
   row is not pending, it is absent.

⚠️ Fixing only the first would leave the second waiting on any probe that
genuinely never answers, which is a real state: a camera that times out on one
table three times running gets no card for the life of the entry. Fixing only
the second would still mislabel every `_DHCloudUpgrade_`-less device as silent
in diagnostics, where the field is read to decide what a model does. Both.
"""

import pytest

from custom_components.dahua import DahuaDataUpdateCoordinator
from custom_components.dahua.coordinator import (
    MAX_CAPABILITY_RETRIES,
    RETRYABLE_CAPABILITIES,
)
from custom_components.dahua.rpc2 import DahuaRpc2Client, Rpc2TableAbsent


def _coordinator(refusals=None, retries=None):
    c = object.__new__(DahuaDataUpdateCoordinator)
    c._probe_refusals = dict(refusals or {})
    c._capability_retries = dict(retries or {})
    return c


def _silent(name):
    return {name: {"answered": False, "status": None, "error": "TimeoutError"}}


# A capability that really is re-asked, so these tests are not written against
# a name the retry loop has never heard of.
RETRYABLE = sorted(RETRYABLE_CAPABILITIES)[0]


# --- the reader: a reply with no table ---------------------------------------


async def test_a_reply_without_the_table_raises_the_answered_kind():
    """⚠️ Not a bare `ValueError`. The type is the whole fix: it is what tells
    the probe bookkeeping that the device replied."""
    client = object.__new__(DahuaRpc2Client)
    client._session_id = "s1"

    async def get_config(params):
        # What his devices actually send back: a reply, and no such table.
        return {}

    client.get_config = get_config

    with pytest.raises(Rpc2TableAbsent):
        await client.get_cloud_upgrade_info()


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"table": None},
        {"table": []},
        {"table": "not a list"},
        {"table": [None]},
        {"table": ["not a dict"]},
    ],
)
async def test_every_shape_of_missing_table_is_the_answered_kind(params):
    client = object.__new__(DahuaRpc2Client)
    client._session_id = "s1"

    async def get_config(_params):
        return params

    client.get_config = get_config

    with pytest.raises(Rpc2TableAbsent):
        await client.get_cloud_upgrade_info()


async def test_a_table_that_is_there_is_still_returned():
    """The fix must not turn a working read into a refusal."""
    client = object.__new__(DahuaRpc2Client)
    client._session_id = "s1"

    async def get_config(_params):
        return {"table": [{"Version": "2.800.000"}]}

    client.get_config = get_config

    assert await client.get_cloud_upgrade_info() == {"Version": "2.800.000"}


def test_the_absent_kind_is_still_a_value_error():
    """Existing handlers catch `ValueError`, and this must not slip past them."""
    assert issubclass(Rpc2TableAbsent, ValueError)


def test_the_absent_kind_declares_that_the_device_answered():
    assert Rpc2TableAbsent("x").answered is True


# --- which probes are worth waiting for ---------------------------------------


def test_a_probe_with_retries_left_may_still_answer():
    c = _coordinator(_silent(RETRYABLE), {RETRYABLE: MAX_CAPABILITY_RETRIES - 1})

    assert c.probes_that_may_still_answer() == {RETRYABLE}


def test_a_probe_that_has_never_been_retried_may_still_answer():
    """No entry in `_capability_retries` means the loop has not reached it
    yet, which is the most pending a probe can be."""
    c = _coordinator(_silent(RETRYABLE))

    assert c.probes_that_may_still_answer() == {RETRYABLE}


def test_a_probe_that_has_spent_its_budget_may_not():
    """⭐ kmich's case. `_async_retry_capabilities` skips a name once
    `spent >= MAX_CAPABILITY_RETRIES`, so nothing will ever ask again."""
    c = _coordinator(_silent(RETRYABLE), {RETRYABLE: MAX_CAPABILITY_RETRIES})

    assert c.probes_that_may_still_answer() == set()
    # ⚠ And it is still unanswered, which is a different question and stays
    # true. Diagnostics reports what the device did; the card asks what is
    # still to come.
    assert c.probes_that_did_not_answer() == {RETRYABLE}


def test_a_probe_nothing_retries_may_not_either():
    """Not in `RETRYABLE_CAPABILITIES`, so it is asked once at setup and never
    again. Waiting for it is waiting for nothing."""
    c = _coordinator(_silent("a_capability_no_retry_loop_knows"))

    assert c.probes_that_may_still_answer() == set()


def test_an_answered_refusal_is_in_neither_set():
    c = _coordinator({"coaxial_control": {"answered": True, "status": 400}})

    assert c.probes_that_did_not_answer() == set()
    assert c.probes_that_may_still_answer() == set()


def test_a_coordinator_that_has_recorded_nothing_is_waiting_for_nothing():
    """`object.__new__` skips `__init__`, and a dozen tests build coordinators
    that way, so neither attribute may be assumed to exist."""
    c = object.__new__(DahuaDataUpdateCoordinator)

    assert c.probes_that_may_still_answer() == set()


# --- and the two halves meet ---------------------------------------------------


def test_the_cloud_upgrade_probe_no_longer_reports_silence(monkeypatch):
    """⭐⭐ End to end on the exact reported case: a device that replies
    without `_DHCloudUpgrade_` must not look like one that said nothing.

    Driven through `_note_probe_refusal` with the real exception the real
    reader raises, rather than asserting the two halves separately and
    trusting them to meet.
    """
    c = _coordinator()

    client = object.__new__(DahuaRpc2Client)
    client._session_id = "s1"

    async def get_config(_params):
        return {"params": "but no table"}

    client.get_config = get_config

    async def run():
        try:
            await client.get_cloud_upgrade_info()
        except Exception as probe_error:  # the probe's own handler
            c._note_probe_refusal("cloud_upgrade", probe_error)

    import asyncio

    asyncio.run(run())

    assert c._probe_refusals["cloud_upgrade"]["answered"] is True
    assert c.probes_that_did_not_answer() == set()
    assert c.probes_that_may_still_answer() == set()


# --- the final attempt is not over until the reply lands ----------------------
#
# ⭐⭐ @rroller on #1125, both findings below. The first fix above left a hole
# at the exact moment it was built for, and the second had no production
# trigger at all. Driven here through the real retry loop, the real probe
# bookkeeping, a real `hass` and a real registry, because every one of those
# was where the hole was: the two halves each passed their own unit test.

import asyncio
from datetime import timedelta
from types import SimpleNamespace

from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    MockEntity,
    MockEntityPlatform,
    async_fire_time_changed,
)

from custom_components.dahua import coordinator as coordinator_module
from custom_components.dahua import late, migrate


async def _let_the_delayed_scan_run(hass, drain=True):
    """Advance past the recheck's coalescing delay and let it scan.

    ⚠ `drain=False` where a registration is deliberately held open.
    `async_block_till_done` waits for that add task, so draining would be a
    deadlock dressed up as a nine second timeout. Yielding instead is enough:
    the scan itself awaits nothing, so it runs to the end the moment the
    timer fires it.
    """
    for _ in range(5):
        if drain:
            await hass.async_block_till_done()
        else:
            await asyncio.sleep(0)
    async_fire_time_changed(
        hass,
        dt_util.utcnow()
        + timedelta(seconds=migrate.LEFTOVER_RECHECK_SETTLE_SECONDS + 1),
    )
    for _ in range(5):
        if drain:
            await hass.async_block_till_done()
        else:
            await asyncio.sleep(0)


from .test_a_card_for_rows_nothing_is_behind import (
    DOMAIN,
    _entry_with_rows,
    _flow,
    _issue,
)

PROBE = "a_capability_under_test"


def _silent_refusal():
    return {PROBE: {"answered": False, "status": None, "error": "TimeoutError"}}


class _Probing:
    """A coordinator with the real retry machinery and the real probe records.

    Borrowed rather than reimplemented: a double that models the retry loop
    cannot prove anything about the retry loop. What is supplied here is only
    what the loop and the leftover gate read off a coordinator.
    """

    probes_that_did_not_answer = DahuaDataUpdateCoordinator.probes_that_did_not_answer
    probes_that_may_still_answer = (
        DahuaDataUpdateCoordinator.probes_that_may_still_answer
    )
    _note_probe_answered = DahuaDataUpdateCoordinator._note_probe_answered
    _notify_late_work_settled = DahuaDataUpdateCoordinator._notify_late_work_settled
    _async_retry_unanswered_capabilities = (
        DahuaDataUpdateCoordinator._async_retry_unanswered_capabilities
    )

    def __init__(self, hass, entry, channel=0, probe=None, refusals=None):
        self.hass = hass
        self.config_entry = entry
        self._probe_refusals = _silent_refusal() if refusals is None else dict(refusals)
        self._capability_retries = {}
        self._channel = channel
        self._address = "10.0.0.230"
        self.initialized = True
        self.last_update_success = True
        self.subentry_id = None
        self.asked = 0
        self._probe = probe

    def get_channel(self):
        return self._channel

    def get_serial_number(self):
        return "SER_{0}".format(self._channel)

    async def _async_probe(self):
        self.asked += 1
        if self._probe is None:
            return False
        return await self._probe(self)


@pytest.fixture
def _only_the_probe_under_test(monkeypatch):
    monkeypatch.setattr(
        coordinator_module,
        "RETRYABLE_CAPABILITIES",
        {PROBE: _Probing._async_probe},
    )


async def test_the_last_attempt_is_still_pending_while_it_is_in_the_air(
    hass, _only_the_probe_under_test
):
    """⭐ The attempt is charged BEFORE the await, so without the in-flight set
    the budget reads as spent while the request is still running."""
    entry = MockConfigEntry(domain="dahua", data={"address": "10.0.0.230"})
    entry.add_to_hass(hass)
    held, reached, seen = asyncio.Event(), asyncio.Event(), {}

    async def probe(coordinator):
        if coordinator.asked < MAX_CAPABILITY_RETRIES:
            return False
        seen["retries"] = dict(coordinator._capability_retries)
        seen["pending"] = coordinator.probes_that_may_still_answer()
        reached.set()
        await held.wait()
        return False

    coordinator = _Probing(hass, entry, probe=probe)
    for _ in range(MAX_CAPABILITY_RETRIES - 1):
        await coordinator._async_retry_unanswered_capabilities()
    last = asyncio.ensure_future(coordinator._async_retry_unanswered_capabilities())
    # ⚠ Waited for, not slept past. A fixed number of yields is a guess about
    # how many awaits the loop takes to reach the probe, and the guess being
    # wrong reads as a nine second timeout rather than a failed assertion.
    await asyncio.wait_for(reached.wait(), 5)

    # The budget reads as spent and the probe has not answered yet. Both true.
    assert seen["retries"][PROBE] == MAX_CAPABILITY_RETRIES
    assert seen["pending"] == {PROBE}

    held.set()
    await last
    # And once it is back with nothing to show for it, the name is gone.
    assert coordinator.probes_that_may_still_answer() == set()


async def test_a_probe_that_raises_does_not_stay_pending_for_ever(
    hass, _only_the_probe_under_test
):
    """⚠ The in-flight set is discarded in `finally`, not after the await. A
    name left in it would hold the gate shut for the life of the entry, which
    is precisely the fault this whole change exists to remove."""
    entry = MockConfigEntry(domain="dahua", data={"address": "10.0.0.230"})
    entry.add_to_hass(hass)

    async def probe(_coordinator):
        raise RuntimeError("not one of PROBE_FAILED")

    coordinator = _Probing(hass, entry, probe=probe)

    with pytest.raises(RuntimeError):
        await coordinator._async_retry_unanswered_capabilities()

    assert coordinator.probes_that_may_still_answer() == {
        PROBE
    }, "two attempts left, so it is pending for the ordinary reason"
    coordinator._capability_retries[PROBE] = MAX_CAPABILITY_RETRIES
    assert coordinator.probes_that_may_still_answer() == set()


async def test_the_card_is_not_offered_while_the_last_probe_is_in_the_air(
    hass, _only_the_probe_under_test
):
    """⭐ rroller's reproduction, against the real registry: he paused the
    final probe and the offer handed over a row it had withheld a moment
    earlier."""
    entry, _made = await _entry_with_rows(hass)
    held, reached, offered = asyncio.Event(), asyncio.Event(), {}

    async def probe(coordinator):
        if coordinator.asked < MAX_CAPABILITY_RETRIES:
            return False
        offered["found"] = await migrate.async_offer_leftover_rows(
            hass, entry, [coordinator]
        )
        offered["issue"] = _issue(hass, entry)
        reached.set()
        await held.wait()
        return False

    coordinator = _Probing(hass, entry, probe=probe)
    entry.runtime_data = {0: coordinator}
    for _ in range(MAX_CAPABILITY_RETRIES - 1):
        await coordinator._async_retry_unanswered_capabilities()
    last = asyncio.ensure_future(coordinator._async_retry_unanswered_capabilities())
    # ⚠ Waited for, not slept past. A fixed number of yields is a guess about
    # how many awaits the loop takes to reach the probe, and the guess being
    # wrong reads as a nine second timeout rather than a failed assertion.
    await asyncio.wait_for(reached.wait(), 5)

    assert offered["found"] == 0
    assert offered["issue"] is None
    held.set()
    await last


async def test_the_repair_flow_will_not_confirm_while_the_last_probe_is_in_the_air(
    hass, _only_the_probe_under_test
):
    """⭐ Named by rroller: "Repair confirmation uses this same health gate, so
    it can accept removal while the capability is unresolved." A card raised
    before the retries began must not be actionable during one."""
    entry, made = await _entry_with_rows(hass)
    settled = _Probing(hass, entry, refusals={})
    entry.runtime_data = {0: settled}
    assert await migrate.async_offer_leftover_rows(hass, entry, [settled]) == 1

    flow = _flow(hass, entry)
    assert (await flow.async_step_init())["type"] == "form", (
        "the form is opened while the entry is healthy, which is the case "
        "rroller described: the card outlives the state it was raised in"
    )

    held, reached, result = asyncio.Event(), asyncio.Event(), {}

    async def probe(_coordinator):
        if _coordinator.asked < MAX_CAPABILITY_RETRIES:
            return False
        # ⭐ The button, pressed during the paused final attempt.
        result["confirm"] = await flow.async_step_confirm({})
        reached.set()
        await held.wait()
        return False

    coordinator = _Probing(hass, entry, probe=probe)
    entry.runtime_data = {0: coordinator}
    for _ in range(MAX_CAPABILITY_RETRIES - 1):
        await coordinator._async_retry_unanswered_capabilities()
    last = asyncio.ensure_future(coordinator._async_retry_unanswered_capabilities())
    # ⚠ Waited for, not slept past. A fixed number of yields is a guess about
    # how many awaits the loop takes to reach the probe, and the guess being
    # wrong reads as a nine second timeout rather than a failed assertion.
    await asyncio.wait_for(reached.wait(), 5)

    assert result["confirm"]["type"] == "abort", "it must delete nothing"
    assert er.async_get(hass).async_get(made["switch.gone"].entity_id) is not None
    held.set()
    await last


# --- and something has to ask again once the waiting ends ---------------------


async def test_the_card_appears_when_the_budget_runs_out_with_no_second_offer(
    hass, _only_the_probe_under_test
):
    """⭐ rroller's second finding. The offer happens once, in
    `async_setup_entry`, when a retryable probe still has its whole budget and
    so rightly suppresses the card. Nothing re-asked afterwards, so the card
    stayed absent for ever, which is #1114 again by another road. The first
    fix's test hid this by calling the offer a second time by hand; there is
    no second call here.
    """
    entry, _made = await _entry_with_rows(hass)
    coordinator = _Probing(hass, entry)
    entry.runtime_data = {0: coordinator}
    migrate.async_recheck_leftover_rows_when_late_work_settles(
        hass, entry, [coordinator]
    )

    assert await migrate.async_offer_leftover_rows(hass, entry, [coordinator]) == 0
    assert _issue(hass, entry) is None, "the probe still has its whole budget"

    for _ in range(MAX_CAPABILITY_RETRIES + 2):
        await coordinator._async_retry_unanswered_capabilities()
    await _let_the_delayed_scan_run(hass)

    assert coordinator.asked == MAX_CAPABILITY_RETRIES
    assert _issue(hass, entry) is not None


async def test_the_recheck_covers_every_channel_of_the_entry(
    hass, _only_the_probe_under_test
):
    """⚠ A recorder. The gate requires every channel to be healthy, so a
    recheck handed only the coordinator that happened to settle would pass a
    single camera and be wrong on a recorder."""
    entry, _made = await _entry_with_rows(hass)
    first = _Probing(hass, entry)
    second = _Probing(hass, entry)
    # ⚠ Both in `runtime_data`, because the recheck reads the entry's live
    # coordinators when it fires rather than the list setup handed it.
    entry.runtime_data = {0: first, 1: second}
    migrate.async_recheck_leftover_rows_when_late_work_settles(hass, entry, [first])

    for _ in range(MAX_CAPABILITY_RETRIES):
        await first._async_retry_unanswered_capabilities()
    await _let_the_delayed_scan_run(hass)
    assert _issue(hass, entry) is None, "the other channel is still being asked"

    for _ in range(MAX_CAPABILITY_RETRIES):
        await second._async_retry_unanswered_capabilities()
    await _let_the_delayed_scan_run(hass)
    assert _issue(hass, entry) is not None


async def test_a_rebuild_that_is_still_running_holds_the_card_back(hass):
    """⭐ The other half of "settled". A probe that ANSWERS leaves
    `probes_that_did_not_answer` the instant it returns, but the entities it
    gates are built by a task `late.py` starts after the poll. Between the two
    the question reads as resolved and the rows are still empty, which is the
    one state in which deleting them is wrong.
    """
    entry, _made = await _entry_with_rows(hass)
    coordinator = _Probing(hass, entry, refusals={})
    entry.runtime_data = {0: coordinator}
    migrate.async_recheck_leftover_rows_when_late_work_settles(
        hass, entry, [coordinator]
    )

    building = asyncio.Event()
    calls = []

    async def build(_coordinator):
        calls.append(1)
        if len(calls) > 1:
            await building.wait()
        return []

    await late.async_add_and_keep_watching(
        hass, entry, build, lambda *a, **k: None, lambda e: e.runtime_data
    )
    late.async_notify_capability_discovered(
        hass, entry.entry_id, coordinator.get_serial_number()
    )
    await asyncio.sleep(0)

    assert late.async_rebuilds_outstanding(hass, entry.entry_id) == 1
    assert await migrate.async_offer_leftover_rows(hass, entry, [coordinator]) == 0
    assert _issue(hass, entry) is None

    building.set()
    await hass.async_block_till_done()
    assert late.async_rebuilds_outstanding(hass, entry.entry_id) == 0
    assert _issue(hass, entry) is None, (
        "⚠ not yet. The entities this rebuild added have had their add "
        "SCHEDULED, and the scan does not await, so a scan here would read "
        "their rows as leftovers"
    )

    await _let_the_delayed_scan_run(hass)

    # ⭐ And the rebuild finishing is what offers the card. Nothing polls.
    assert _issue(hass, entry) is not None


async def _watching(hass, entry, build, add=None):
    """Wire the late rebuild the way a platform does."""
    await late.async_add_and_keep_watching(
        hass,
        entry,
        build,
        add if add is not None else (lambda *a, **k: None),
        lambda e: e.runtime_data,
    )


async def test_a_failed_late_build_keeps_the_rows_it_did_not_create(hass):
    """⭐⭐ @rroller on #1125, and the previous version of this test asserted
    the opposite. A build that raised did not create its entities, so their
    rows are empty because of a fault HERE. That is the same
    misclassification the platform-setup guard exists to refuse, and offering
    them for deletion is how a tidy-up card deletes something real.

    The failure is still reported rather than left as an "exception was never
    retrieved" line naming neither entry nor channel; what changed is that it
    is also remembered.
    """
    entry, _made = await _entry_with_rows(hass)
    coordinator = _Probing(hass, entry, refusals={})
    entry.runtime_data = {0: coordinator}
    calls = []

    async def build(_coordinator):
        calls.append(1)
        if len(calls) > 1:
            raise RuntimeError("a platform build that blew up")
        return []

    await _watching(hass, entry, build)
    migrate.async_recheck_leftover_rows_when_late_work_settles(
        hass, entry, [coordinator]
    )
    late.async_notify_capability_discovered(
        hass, entry.entry_id, coordinator.get_serial_number()
    )
    await _let_the_delayed_scan_run(hass)

    assert (
        late.async_rebuilds_outstanding(hass, entry.entry_id) == 0
    ), "the count still comes down, so one failure cannot wedge the counter"
    assert late.async_rebuilds_failed(hass, entry.entry_id) is True
    assert _issue(hass, entry) is None, "nothing may be offered for deletion"
    assert await migrate.async_offer_leftover_rows(hass, entry, [coordinator]) == 0


async def test_a_channel_that_rebuilds_cleanly_afterwards_is_eligible_again(hass):
    """⚠ Otherwise one transient build error shuts the card for the life of
    the entry, which is the fault this whole branch is about, arrived at from
    the other direction."""
    entry, _made = await _entry_with_rows(hass)
    coordinator = _Probing(hass, entry, refusals={})
    entry.runtime_data = {0: coordinator}
    calls = []

    async def build(_coordinator):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("a platform build that blew up")
        return []

    await _watching(hass, entry, build)
    migrate.async_recheck_leftover_rows_when_late_work_settles(
        hass, entry, [coordinator]
    )
    key = coordinator.get_serial_number()
    late.async_notify_capability_discovered(hass, entry.entry_id, key)
    await _let_the_delayed_scan_run(hass)
    assert _issue(hass, entry) is None

    late.async_notify_capability_discovered(hass, entry.entry_id, key)
    await _let_the_delayed_scan_run(hass)

    assert late.async_rebuilds_failed(hass, entry.entry_id) is False
    assert _issue(hass, entry) is not None


async def test_unloading_forgets_a_failed_rebuild(hass):
    """It is evidence about a run that has ended. Carrying it across a reload
    would keep the card shut for ever on a channel that is now fine."""
    entry, _made = await _entry_with_rows(hass)
    coordinator = _Probing(hass, entry, refusals={})
    entry.runtime_data = {0: coordinator}

    async def build(_coordinator):
        raise RuntimeError("a platform build that blew up")

    migrate.async_recheck_leftover_rows_when_late_work_settles(
        hass, entry, [coordinator]
    )
    with pytest.raises(RuntimeError):
        await _watching(hass, entry, build)
    late._note_failed(hass, entry.entry_id, object(), "SER_0", True)
    assert late.async_rebuilds_failed(hass, entry.entry_id) is True

    for unload in list(entry._on_unload or []):
        unload()

    assert late.async_rebuilds_failed(hass, entry.entry_id) is False


# --- and the add itself is not over when async_add_entities returns ----------


class _Registering:
    """An entity as far as the counting is concerned: the two hooks Home
    Assistant calls, and nothing else.

    ⚠ The production code wraps exactly these, because `async_add_entities`
    returns once the add has been SCHEDULED and the task it schedules is
    tracked nowhere reachable after setup. `add_to_platform_start` always
    runs first and then exactly one of them, which is what makes one release
    per entity correct.
    """

    def __init__(self, unique_id):
        self.unique_id = unique_id
        self.finished = self.aborted = False
        # What Home Assistant leaves on an entity it is adding, and what the
        # "was this skip intentional" question reads.
        self.entity_id = None
        self.registry_entry = None

    async def add_to_platform_finish(self):
        self.finished = True

    def add_to_platform_abort(self):
        self.aborted = True


async def test_the_rebuild_is_not_settled_until_the_entities_are_registered(hass):
    """⭐⭐ @rroller on #1125, after the first two attempts: "a registration
    coroutine paused beyond five seconds leaves its row eligible for
    deletion". He is right that no number closes this.
    `DahuaIlluminator.async_added_to_hass` awaits startup restoration with
    device reads in it, so an add is not bounded by anything anyone picks.
    """
    entry, _made = await _entry_with_rows(hass)
    coordinator = _Probing(hass, entry, refusals={})
    entry.runtime_data = {0: coordinator}
    entity, calls = _Registering("SER_gone"), []

    async def build(_coordinator):
        calls.append(1)
        return [entity] if len(calls) > 1 else []

    added = []
    await _watching(hass, entry, build, lambda e, **k: added.extend(e))
    migrate.async_recheck_leftover_rows_when_late_work_settles(
        hass, entry, [coordinator]
    )
    late.async_notify_capability_discovered(
        hass, entry.entry_id, coordinator.get_serial_number()
    )
    # The rebuild task is over and the add has only been scheduled: Home
    # Assistant has not called either hook yet, which is the whole window.
    await _let_the_delayed_scan_run(hass)

    assert added == [entity]
    assert late.async_rebuilds_outstanding(hass, entry.entry_id) == 1
    assert (
        _issue(hass, entry) is None
    ), "the row belongs to an entity that is still being registered"
    assert await migrate.async_offer_leftover_rows(hass, entry, [coordinator]) == 0

    # Home Assistant finishes the registration, and only now is it settled.
    await entity.add_to_platform_finish()
    assert entity.finished is True, "the real hook still runs"
    await _let_the_delayed_scan_run(hass)

    assert late.async_rebuilds_outstanding(hass, entry.entry_id) == 0
    assert _issue(hass, entry) is not None


async def test_the_real_platform_callback_is_what_releases_it(hass):
    """⭐⭐ The same claim as the test above, but against Home Assistant's own
    `_async_schedule_add_entities_for_entry` and a real entity, which is how
    @rroller reproduced it. The double above proves the wrapper; this proves
    the wrapper is on the path Home Assistant actually takes, and that a
    registration held open past any timer keeps the row safe.
    """
    entry, made = await _entry_with_rows(hass)
    coordinator = _Probing(hass, entry, refusals={})
    entry.runtime_data = {0: coordinator}

    registering = asyncio.Event()

    class _Slow(MockEntity):
        async def async_added_to_hass(self):
            # Where DahuaIlluminator awaits startup restoration, device reads
            # included. There is no number that bounds this.
            await registering.wait()

    platform = MockEntityPlatform(hass, domain="switch", platform_name=DOMAIN)
    platform.config_entry = entry
    platform._setup_complete = True

    entity, calls = _Slow(unique_id="SER_late", name="late"), []

    async def build(_coordinator):
        calls.append(1)
        return [entity] if len(calls) > 1 else []

    await _watching(
        hass,
        entry,
        build,
        lambda entities, **kwargs: platform._async_schedule_add_entities_for_entry(
            entities
        ),
    )
    migrate.async_recheck_leftover_rows_when_late_work_settles(
        hass, entry, [coordinator]
    )
    late.async_notify_capability_discovered(
        hass, entry.entry_id, coordinator.get_serial_number()
    )
    await _let_the_delayed_scan_run(hass, drain=False)

    assert (
        late.async_rebuilds_outstanding(hass, entry.entry_id) == 1
    ), "the add is scheduled and `async_added_to_hass` has not returned"
    assert _issue(hass, entry) is None
    assert er.async_get(hass).async_get(made["switch.gone"].entity_id) is not None

    registering.set()
    await _let_the_delayed_scan_run(hass)

    assert late.async_rebuilds_outstanding(hass, entry.entry_id) == 0
    assert entity.entity_id and hass.states.get(entity.entity_id) is not None
    assert _issue(hass, entry) is not None


async def test_one_platform_succeeding_does_not_clear_another_s_failure(hass):
    """⭐⭐ @rroller on #1125. Ten platforms each call
    `async_add_and_keep_watching` and all ten listen to the same discovery
    signal, so one channel is rebuilt by ten different `build` functions.
    Keyed by channel alone, the first to succeed cleared a failure belonging
    to another, and the row it never created was offered for deletion.
    """
    entry, made = await _entry_with_rows(hass)
    coordinator = _Probing(hass, entry, refusals={})
    entry.runtime_data = {0: coordinator}
    broken, working = [], []

    async def breaks(_coordinator):
        broken.append(1)
        if len(broken) > 1:
            raise RuntimeError("this platform's build blew up")
        return []

    async def works(_coordinator):
        working.append(1)
        return []

    await _watching(hass, entry, breaks)
    await _watching(hass, entry, works)
    migrate.async_recheck_leftover_rows_when_late_work_settles(
        hass, entry, [coordinator]
    )
    late.async_notify_capability_discovered(
        hass, entry.entry_id, coordinator.get_serial_number()
    )
    await _let_the_delayed_scan_run(hass)

    assert len(broken) == 2 and len(working) == 2, "both watchers rebuilt"
    assert late.async_rebuilds_failed(hass, entry.entry_id) is True
    assert _issue(hass, entry) is None
    assert er.async_get(hass).async_get(made["switch.gone"].entity_id) is not None


async def test_a_registration_that_raises_is_remembered_not_just_released(hass):
    """⭐⭐ @rroller on #1125, with a real `MockEntityPlatform` and an entity
    whose `async_added_to_hass` raises. The error happens inside the add task
    Home Assistant scheduled, so the build wrapper never sees it: the count
    came down, nothing was recorded, and the row that entity was going to
    fill was offered for deletion.
    """
    entry, made = await _entry_with_rows(hass)
    coordinator = _Probing(hass, entry, refusals={})
    entry.runtime_data = {0: coordinator}

    class _Breaks(MockEntity):
        async def async_added_to_hass(self):
            raise RuntimeError("registration blew up")

    platform = MockEntityPlatform(hass, domain="switch", platform_name=DOMAIN)
    platform.config_entry = entry
    platform._setup_complete = True

    entity, calls = _Breaks(unique_id="SER_broken", name="broken"), []

    async def build(_coordinator):
        calls.append(1)
        return [entity] if len(calls) > 1 else []

    await _watching(
        hass,
        entry,
        build,
        lambda entities, **kwargs: platform._async_schedule_add_entities_for_entry(
            entities
        ),
    )
    migrate.async_recheck_leftover_rows_when_late_work_settles(
        hass, entry, [coordinator]
    )
    late.async_notify_capability_discovered(
        hass, entry.entry_id, coordinator.get_serial_number()
    )
    await _let_the_delayed_scan_run(hass)

    assert (
        late.async_rebuilds_outstanding(hass, entry.entry_id) == 0
    ), "the count still comes down, or one bad add wedges the counter"
    assert late.async_rebuilds_failed(hass, entry.entry_id) is True
    assert _issue(hass, entry) is None
    assert er.async_get(hass).async_get(made["switch.gone"].entity_id) is not None


async def test_a_disabled_row_aborting_is_not_a_failure(hass):
    """⚠ The other half of rroller's note. Home Assistant aborts an add for a
    disabled registry row on purpose, and `leftover_rows` excludes disabled
    rows anyway. Recording that as a failure would let one entity the user
    switched off suppress the card for the life of the entry, which is the
    over-correction this whole branch exists to undo.
    """
    entry, _made = await _entry_with_rows(hass)
    coordinator = _Probing(hass, entry, refusals={})
    entry.runtime_data = {0: coordinator}
    entity, calls = _Registering("SER_disabled"), []
    entity.registry_entry = SimpleNamespace(disabled=True)

    async def build(_coordinator):
        calls.append(1)
        return [entity] if len(calls) > 1 else []

    await _watching(hass, entry, build)
    migrate.async_recheck_leftover_rows_when_late_work_settles(
        hass, entry, [coordinator]
    )
    late.async_notify_capability_discovered(
        hass, entry.entry_id, coordinator.get_serial_number()
    )
    await hass.async_block_till_done()
    entity.add_to_platform_abort()
    await _let_the_delayed_scan_run(hass)

    assert late.async_rebuilds_failed(hass, entry.entry_id) is False
    assert _issue(hass, entry) is not None


async def test_an_id_someone_else_already_holds_is_not_a_failure_either(hass):
    """The duplicate skip. Something live is behind the row, so it does not
    read as a leftover and nothing here is at risk."""
    entry, made = await _entry_with_rows(hass)
    coordinator = _Probing(hass, entry, refusals={})
    entry.runtime_data = {0: coordinator}
    entity, calls = _Registering("SER_live"), []
    entity.entity_id = made["switch.live"].entity_id

    async def build(_coordinator):
        calls.append(1)
        return [entity] if len(calls) > 1 else []

    await _watching(hass, entry, build)
    migrate.async_recheck_leftover_rows_when_late_work_settles(
        hass, entry, [coordinator]
    )
    late.async_notify_capability_discovered(
        hass, entry.entry_id, coordinator.get_serial_number()
    )
    await hass.async_block_till_done()
    entity.add_to_platform_abort()
    await _let_the_delayed_scan_run(hass)

    assert late.async_rebuilds_failed(hass, entry.entry_id) is False
    assert _issue(hass, entry) is not None


async def test_an_abort_that_leaves_the_row_empty_is_a_failure(hass):
    """And the error aborts, which are every other path in
    `_async_add_entity`. The entity is not there and nothing says the user
    meant that, so the row it was going to fill stays out of reach."""
    entry, made = await _entry_with_rows(hass)
    coordinator = _Probing(hass, entry, refusals={})
    entry.runtime_data = {0: coordinator}
    entity, calls = _Registering("SER_gone"), []
    entity.entity_id = made["switch.gone"].entity_id

    async def build(_coordinator):
        calls.append(1)
        return [entity] if len(calls) > 1 else []

    await _watching(hass, entry, build)
    migrate.async_recheck_leftover_rows_when_late_work_settles(
        hass, entry, [coordinator]
    )
    late.async_notify_capability_discovered(
        hass, entry.entry_id, coordinator.get_serial_number()
    )
    await hass.async_block_till_done()
    entity.add_to_platform_abort()
    await _let_the_delayed_scan_run(hass)

    assert late.async_rebuilds_failed(hass, entry.entry_id) is True
    assert _issue(hass, entry) is None


async def test_an_aborted_registration_releases_it_too(hass):
    """Home Assistant aborts an add for a duplicate entity id or a disabled
    row, and calls `add_to_platform_abort` instead. Counting only the finish
    would hold the gate shut for ever on an entity that is never coming."""
    entry, _made = await _entry_with_rows(hass)
    coordinator = _Probing(hass, entry, refusals={})
    entry.runtime_data = {0: coordinator}
    entity, calls = _Registering("SER_gone"), []

    async def build(_coordinator):
        calls.append(1)
        return [entity] if len(calls) > 1 else []

    await _watching(hass, entry, build)
    late.async_notify_capability_discovered(
        hass, entry.entry_id, coordinator.get_serial_number()
    )
    await hass.async_block_till_done()
    assert late.async_rebuilds_outstanding(hass, entry.entry_id) == 1

    entity.add_to_platform_abort()

    assert entity.aborted is True
    assert late.async_rebuilds_outstanding(hass, entry.entry_id) == 0


async def test_setting_an_entry_up_is_what_registers_the_recheck(hass, monkeypatch):
    """⚠⚠ Every test above registers the listener itself, so every one of them
    passes with the production wiring deleted. That is the shape of rroller's
    P2 in the first place: a method nothing called.

    Driven through the real `async_setup_entry`, and asserted on the effect
    rather than on the dispatcher's internals: the signal has to reach the
    offer, with the whole entry's coordinators rather than the one that
    happened to settle.
    """
    from .test_setup_lifecycle import _entry, _no_platforms, _try_setup, _working

    asked = []

    async def record(_hass, _entry, coordinators):
        asked.append(list(coordinators))
        return 0

    monkeypatch.setattr(migrate, "async_offer_leftover_rows", record)
    entry = _entry(hass)
    starts, forward, unload = _no_platforms(hass)
    with _working(), starts, forward, unload:
        assert await _try_setup(hass, entry) is True
    assert len(asked) == 1, "setup offers once, which is the pre-existing call"

    late.async_notify_late_work_settled(hass, entry.entry_id)
    await _let_the_delayed_scan_run(hass)

    assert len(asked) == 2, "nothing re-offers when a probe finally settles"
    assert asked[1] == asked[0], "the recheck must see every channel of the entry"
    assert asked[1] == list(entry.runtime_data.values())
    # ⚠ Not asserted here: that unloading removes it. It is registered with
    # `entry.async_on_unload`, and those run from the config entry manager
    # rather than from `async_unload_entry`, so a test calling that function
    # directly would be making a claim about Home Assistant.
