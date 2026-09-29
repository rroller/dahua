"""A repair that cannot be acted on is noise, so there are only two."""

import pytest
from homeassistant.components.repairs import ConfirmRepairFlow
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components import dahua as dahua_module
from custom_components.dahua import host as host_module
from custom_components.dahua import (
    ISSUE_HTTP_DEAD_HTTPS_AVAILABLE,
    ISSUE_SIBLINGS_REMAIN,
    ISSUE_UNREACHABLE,
    UNREACHABLE_AFTER_FAILURES,
    async_record_host_failure,
    async_record_host_success,
)
from custom_components.dahua.const import DOMAIN
from custom_components.dahua.repairs import (
    RELOAD_STAGGER_SECONDS,
    RemoveSiblingsRepairFlow,
    SwitchToHttpsRepairFlow,
    async_create_fix_flow,
)

ADDRESS = "10.0.0.1"


@pytest.fixture(autouse=True)
def _clean_state():
    dahua_module._HOST_FAILURES.clear()
    yield
    dahua_module._HOST_FAILURES.clear()


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """The flow staggers reloads; the suite runs with --timeout=9."""
    async def _instant(_seconds):
        return None

    monkeypatch.setattr("custom_components.dahua.repairs.asyncio.sleep", _instant)


def _entry(hass, *, address=ADDRESS, channel=0, port="80", use_https=None):
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=f"Ch{channel}",
        unique_id=f"SERIAL_{channel}" if channel else "SERIAL",
        data={
            "username": "u", "password": "p", "address": address,
            "port": port, "rtsp_port": "554", "channel": channel,
            "name": f"Ch{channel}", "use_https": use_https,
        },
    )
    entry.add_to_hass(hass)
    return entry


def _probe(monkeypatch, result, counter=None):
    async def fake(address, port, timeout=5.0):
        if counter is not None:
            counter.append((address, port))
        return result

    # Patched on host.py, which is where _async_evaluate_host resolves it.
    # Rebinding it on the package instead left the real probe running: these
    # tests went on passing and the socket guard failed them at teardown,
    # which is a good deal more polite than the alternative.
    monkeypatch.setattr(host_module, "_async_probe_tcp", fake)


def _issue(hass, template, address=ADDRESS):
    return ir.async_get(hass).async_get_issue(DOMAIN, template.format(address))


async def _fail(hass, entry, times):
    for _ in range(times):
        async_record_host_failure(hass, entry.data["address"], entry.entry_id)
    await hass.async_block_till_done()


# --- when a card appears ---------------------------------------------------

async def test_a_single_failure_raises_nothing(hass, monkeypatch):
    """One dropped poll must not put a card in front of the user."""
    _probe(monkeypatch, False)
    entry = _entry(hass)

    await _fail(hass, entry, 1)

    assert _issue(hass, ISSUE_UNREACHABLE) is None


async def test_repeated_failures_raise_one_card(hass, monkeypatch):
    _probe(monkeypatch, False)
    entry = _entry(hass)

    await _fail(hass, entry, UNREACHABLE_AFTER_FAILURES)

    issue = _issue(hass, ISSUE_UNREACHABLE)
    assert issue is not None
    assert issue.severity == ir.IssueSeverity.WARNING
    assert issue.is_fixable is False


async def test_eight_channels_of_one_nvr_get_one_card_not_eight(hass, monkeypatch):
    """The single most-repeated structural complaint about this integration."""
    _probe(monkeypatch, False)
    entries = [_entry(hass, channel=i) for i in range(8)]

    for entry in entries:
        async_record_host_failure(hass, ADDRESS, entry.entry_id)
    await hass.async_block_till_done()

    ours = [k for k in ir.async_get(hass).issues if k[0] == DOMAIN]
    assert len(ours) == 1


async def test_a_trailing_slash_is_the_same_host(hass, monkeypatch):
    _probe(monkeypatch, False)
    _entry(hass)

    for _ in range(3):
        async_record_host_failure(hass, ADDRESS, "a")
    for _ in range(3):
        async_record_host_failure(hass, ADDRESS + "/", "b")
    await hass.async_block_till_done()

    assert len([k for k in ir.async_get(hass).issues if k[0] == DOMAIN]) == 1
    assert _issue(hass, ISSUE_UNREACHABLE) is not None


# --- when it goes away -----------------------------------------------------

async def test_a_success_clears_the_card(hass, monkeypatch):
    _probe(monkeypatch, False)
    entry = _entry(hass)
    await _fail(hass, entry, UNREACHABLE_AFTER_FAILURES)
    assert _issue(hass, ISSUE_UNREACHABLE) is not None

    async_record_host_success(hass, ADDRESS)

    assert _issue(hass, ISSUE_UNREACHABLE) is None
    assert dahua_module._HOST_FAILURES == {}


async def test_one_channel_recovering_clears_the_card_for_the_host(hass, monkeypatch):
    """If any channel answers, the box is up."""
    _probe(monkeypatch, False)
    entries = [_entry(hass, channel=i) for i in range(8)]
    for entry in entries:
        async_record_host_failure(hass, ADDRESS, entry.entry_id)
    await hass.async_block_till_done()

    async_record_host_success(hass, ADDRESS)

    assert _issue(hass, ISSUE_UNREACHABLE) is None


async def test_unloading_the_last_entry_removes_the_card(hass, monkeypatch):
    _probe(monkeypatch, False)
    entry = _entry(hass)
    await _fail(hass, entry, UNREACHABLE_AFTER_FAILURES)
    assert _issue(hass, ISSUE_UNREACHABLE) is not None

    await hass.config_entries.async_remove(entry.entry_id)
    ir.async_delete_issue(hass, DOMAIN, ISSUE_UNREACHABLE.format(ADDRESS))

    assert _issue(hass, ISSUE_UNREACHABLE) is None


# --- choosing between the two cards ---------------------------------------

async def test_https_is_offered_when_443_answers(hass, monkeypatch):
    """The failure we actually hit: port 80 dead, 443 accepting."""
    _probe(monkeypatch, True)
    entry = _entry(hass)

    await _fail(hass, entry, UNREACHABLE_AFTER_FAILURES)

    issue = _issue(hass, ISSUE_HTTP_DEAD_HTTPS_AVAILABLE)
    assert issue is not None
    assert issue.is_fixable is True
    assert issue.translation_placeholders["address"] == ADDRESS
    assert _issue(hass, ISSUE_UNREACHABLE) is None


async def test_the_unreachable_card_is_raised_when_443_is_dead_too(hass, monkeypatch):
    _probe(monkeypatch, False)
    entry = _entry(hass)

    await _fail(hass, entry, UNREACHABLE_AFTER_FAILURES)

    assert _issue(hass, ISSUE_UNREACHABLE) is not None
    assert _issue(hass, ISSUE_HTTP_DEAD_HTTPS_AVAILABLE) is None


async def test_an_entry_already_on_https_is_never_probed(hass, monkeypatch):
    """Nothing to offer someone who is already using HTTPS."""
    calls = []
    _probe(monkeypatch, True, calls)
    entry = _entry(hass, port="443")

    await _fail(hass, entry, UNREACHABLE_AFTER_FAILURES)

    assert calls == []
    assert _issue(hass, ISSUE_HTTP_DEAD_HTTPS_AVAILABLE) is None
    assert _issue(hass, ISSUE_UNREACHABLE) is not None


async def test_the_probe_is_not_repeated_on_every_failure(hass, monkeypatch):
    """A wedged host fails every poll; it must not be probed every poll."""
    calls = []
    _probe(monkeypatch, False, calls)
    entry = _entry(hass)

    await _fail(hass, entry, 30)

    assert len(calls) == 1


async def test_a_host_with_no_entries_left_raises_nothing(hass, monkeypatch):
    """The failures and the evaluation are not the same moment. Recording one
    schedules `_async_evaluate_host` as a task, and an entry can be removed before
    that task runs: a user deleting a camera that has been failing does exactly
    that, and unloading the last entry for a host is the documented case.

    Nothing to say about a host nobody has configured, so nothing is probed and no
    card appears. The early return is also what keeps `entries[0]` below it from
    being an IndexError on an empty list, which would surface as an unhandled task
    exception rather than as anything a user could act on."""
    calls = []
    _probe(monkeypatch, True, calls)

    for _ in range(UNREACHABLE_AFTER_FAILURES):
        async_record_host_failure(hass, ADDRESS, "an-entry-that-is-gone")
    await hass.async_block_till_done()

    assert calls == [], "a host with no entries must not be probed"
    assert _issue(hass, ISSUE_UNREACHABLE) is None
    assert _issue(hass, ISSUE_HTTP_DEAD_HTTPS_AVAILABLE) is None


# --- the fix flow ----------------------------------------------------------

async def test_the_fix_flow_switches_every_entry_for_the_host(hass, monkeypatch):
    entries = [_entry(hass, channel=i) for i in range(3)]
    _entry(hass, address="10.0.0.2", channel=0)
    ir.async_create_issue(
        hass, DOMAIN, ISSUE_HTTP_DEAD_HTTPS_AVAILABLE.format(ADDRESS),
        is_fixable=True, severity=ir.IssueSeverity.WARNING,
        translation_key="http_dead_https_available", data={"address": ADDRESS},
    )

    flow = await async_create_fix_flow(
        hass, ISSUE_HTTP_DEAD_HTTPS_AVAILABLE.format(ADDRESS), {"address": ADDRESS}
    )
    flow.hass = hass
    await flow.async_step_confirm({})

    for entry in entries:
        assert entry.data["port"] == "443"
        assert entry.data["use_https"] is True
    assert _issue(hass, ISSUE_HTTP_DEAD_HTTPS_AVAILABLE) is None


async def test_the_other_hosts_entries_are_left_alone(hass, monkeypatch):
    _entry(hass, channel=0)
    other = _entry(hass, address="10.0.0.2", channel=0)

    flow = SwitchToHttpsRepairFlow({"address": ADDRESS})
    flow.hass = hass
    await flow.async_step_confirm({})

    assert other.data["port"] == "80"


async def test_the_confirm_step_shows_a_form_before_changing_anything(hass):
    entry = _entry(hass)

    flow = SwitchToHttpsRepairFlow({"address": ADDRESS})
    flow.hass = hass
    result = await flow.async_step_confirm()

    assert result["type"] == "form"
    assert result["description_placeholders"]["entries"] == "1"
    assert entry.data["port"] == "80", "changed something before confirmation"


async def test_the_flow_aborts_when_the_entries_are_gone(hass):
    flow = SwitchToHttpsRepairFlow({"address": "10.9.9.9"})
    flow.hass = hass

    result = await flow.async_step_confirm()

    assert result["type"] == "abort"
    assert result["reason"] == "not_configured"


async def test_an_unknown_issue_falls_back_to_confirm(hass):
    flow = await async_create_fix_flow(hass, "something_else", None)
    assert isinstance(flow, ConfirmRepairFlow)


async def test_the_init_step_goes_straight_to_the_confirm_step(hass):
    """Home Assistant enters a repair flow at `init`, not at `confirm`. Every test
    above calls `async_step_confirm` directly, so nothing was checking that the door
    into these flows leads anywhere -- an init step that returned the wrong thing would
    give a card that opens onto nothing."""
    _entry(hass)

    https = SwitchToHttpsRepairFlow({"address": ADDRESS})
    https.hass = hass
    siblings = RemoveSiblingsRepairFlow({"address": ADDRESS})
    siblings.hass = hass

    assert (await https.async_step_init())["step_id"] == "confirm"
    assert (await siblings.async_step_init())["step_id"] == "confirm"


# --- finishing a half-done removal -----------------------------------------
#
# This flow deletes config entries, which is the one irreversible thing in repairs.py,
# and until now no test referenced it at all. The card that offers it was tested; the
# button that acts on it was not.

async def test_a_siblings_card_gets_the_removal_flow(hass):
    flow = await async_create_fix_flow(
        hass, ISSUE_SIBLINGS_REMAIN.format(ADDRESS), {"address": ADDRESS})

    assert isinstance(flow, RemoveSiblingsRepairFlow)


async def test_the_form_lists_what_will_go_and_removes_nothing_yet(hass):
    """The class docstring promises that nothing happens on merely opening the card.
    Since what happens is a deletion, that promise is worth a test: the form names
    every entry it would remove, and all of them are still there afterwards."""
    entries = [_entry(hass, channel=i) for i in range(3)]

    flow = RemoveSiblingsRepairFlow(
        {"address": ADDRESS, "removed": "Ch3", "dependents_note": ""})
    flow.hass = hass
    result = await flow.async_step_confirm()

    assert result["type"] == "form"
    assert result["description_placeholders"]["count"] == "3"
    assert result["description_placeholders"]["titles"] == "Ch0, Ch1, Ch2"
    assert result["description_placeholders"]["removed"] == "Ch3"
    for entry in entries:
        assert entry.entry_id in {e.entry_id
                                 for e in hass.config_entries.async_entries(DOMAIN)}, \
            "an entry was removed before anybody confirmed"


async def test_confirming_removes_every_entry_for_that_recorder(hass):
    """The point of the flow. A recorder that migration left as one entry per channel
    takes as many deletions as it has channels, and this is the one press that
    finishes it."""
    for i in range(3):
        _entry(hass, channel=i)
    ir.async_create_issue(
        hass, DOMAIN, ISSUE_SIBLINGS_REMAIN.format(ADDRESS),
        is_fixable=True, is_persistent=True, severity=ir.IssueSeverity.WARNING,
        translation_key="siblings_remain", data={"address": ADDRESS},
    )

    flow = RemoveSiblingsRepairFlow({"address": ADDRESS})
    flow.hass = hass
    result = await flow.async_step_confirm({})
    await hass.async_block_till_done()

    assert result["type"] == "create_entry"
    assert hass.config_entries.async_entries(DOMAIN) == []
    assert _issue(hass, ISSUE_SIBLINGS_REMAIN) is None, "the card outlived the fix"


async def test_another_recorders_entries_are_left_alone(hass):
    """The one that matters most. This deletes, so matching the wrong address does not
    mean a confusing dialog -- it means somebody else's cameras and their history are
    gone, with no undo."""
    _entry(hass, channel=0)
    other = _entry(hass, address="10.0.0.2", channel=0)

    flow = RemoveSiblingsRepairFlow({"address": ADDRESS})
    flow.hass = hass
    await flow.async_step_confirm({})
    await hass.async_block_till_done()

    remaining = hass.config_entries.async_entries(DOMAIN)
    assert [e.entry_id for e in remaining] == [other.entry_id]
    assert other.data["address"] == "10.0.0.2"


async def test_the_removals_are_staggered(hass, monkeypatch):
    """Eleven simultaneous teardowns is the burst that can wedge a Dahua web server,
    which is why there is a sleep between them rather than a gather. A deliberate
    delay with a comment explaining it should not be removable in silence."""
    waits = []

    async def _record(seconds):
        waits.append(seconds)

    # `repairs.asyncio` is the asyncio module itself, so this patch is global and
    # catches sleeps from anywhere -- Home Assistant's own removal path does a
    # `sleep(0)`. Count the stagger's own value rather than every call, which is the
    # tighter assertion anyway: it fails if the delay is changed to zero.
    monkeypatch.setattr("custom_components.dahua.repairs.asyncio.sleep", _record)
    for i in range(4):
        _entry(hass, channel=i)

    flow = RemoveSiblingsRepairFlow({"address": ADDRESS})
    flow.hass = hass
    await flow.async_step_confirm({})
    await hass.async_block_till_done()

    staggers = [wait for wait in waits if wait == RELOAD_STAGGER_SECONDS]
    assert len(staggers) == 4, (
        "expected one %ss pause per entry removed, saw %s"
        % (RELOAD_STAGGER_SECONDS, waits))
    assert RELOAD_STAGGER_SECONDS > 0


async def test_a_flow_whose_entries_are_already_gone_withdraws_the_card(hass):
    """The last entry can go by hand while the card is open. Unlike the HTTPS flow,
    this one deletes its own issue on the way out: there is nothing left to fix, and a
    card offering to remove nothing is worse than no card."""
    ir.async_create_issue(
        hass, DOMAIN, ISSUE_SIBLINGS_REMAIN.format(ADDRESS),
        is_fixable=True, is_persistent=True, severity=ir.IssueSeverity.WARNING,
        translation_key="siblings_remain", data={"address": ADDRESS},
    )

    flow = RemoveSiblingsRepairFlow({"address": ADDRESS})
    flow.hass = hass
    result = await flow.async_step_confirm()

    assert result["type"] == "abort"
    assert result["reason"] == "not_configured"
    assert _issue(hass, ISSUE_SIBLINGS_REMAIN) is None


async def test_the_dependents_note_reaches_the_form(hass):
    """What referenced the entry that was already removed. It is composed as a whole
    sentence upstream, so the form passes it through untouched."""
    _entry(hass)
    note = "Two automations also referenced the entry you removed."

    flow = RemoveSiblingsRepairFlow({"address": ADDRESS, "dependents_note": note})
    flow.hass = hass
    result = await flow.async_step_confirm()

    assert result["description_placeholders"]["dependents_note"] == note


async def test_the_placeholders_fall_back_rather_than_rendering_none(hass):
    """A card raised before these keys existed, or by a path that omits them, must not
    put the word None on screen. Both defaults are in __init__, and this is what says
    so -- `str(None)` would render literally."""
    _entry(hass)

    flow = RemoveSiblingsRepairFlow({"address": ADDRESS})
    flow.hass = hass
    result = await flow.async_step_confirm()

    placeholders = result["description_placeholders"]
    assert placeholders["removed"] == "the entry"
    assert placeholders["dependents_note"] == ""
    assert "None" not in placeholders["removed"]
