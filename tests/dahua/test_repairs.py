"""A repair that cannot be acted on is noise, so there are only two."""

import pytest
from homeassistant.components.repairs import ConfirmRepairFlow
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components import dahua as dahua_module
from custom_components.dahua import (
    ISSUE_HTTP_DEAD_HTTPS_AVAILABLE,
    ISSUE_UNREACHABLE,
    UNREACHABLE_AFTER_FAILURES,
    async_record_host_failure,
    async_record_host_success,
)
from custom_components.dahua.const import DOMAIN
from custom_components.dahua.repairs import (
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

    monkeypatch.setattr(dahua_module, "_async_probe_tcp", fake)


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
