"""A deleted device should not leave its Repairs card behind.

Two issues are raised per host: `device_unreachable`, and the one offering to
switch a host to HTTPS. Both are per address rather than per entry, because an
NVR carries one entry per channel and eleven identical cards would be worse than
none.

**The cleanup that withdrew them could never run.** It lived in
`async_unload_entry`, guarded by "is this the last entry for the host":

    address = normalize_address(entry.data.get(CONF_ADDRESS))
    if not _entries_for_address(hass, address):
        ...withdraw...

Home Assistant unloads an entry *before* it deletes it, and
`hass.config_entries.async_entries(DOMAIN)` filters by domain with no state
filter, so the entry being unloaded still counted itself. The guard was never
true and the block was dead code. What the user saw was a card for a device they
had deleted, pointing at an address with no entries, offering to reconfigure
nothing, and impossible to dismiss.

It also should not run on a reload, which is the *other* reason
`async_unload_entry` is called: the failure count drives the poll backoff, so
clearing it there would reset the backoff every time somebody saved an option.

So it belongs in `async_remove_entry`, which only means removal, and which Home
Assistant calls after the entry has left its registry.
"""
import pytest
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components import dahua as dahua_module
from custom_components.dahua import (
    ISSUE_HTTP_DEAD_HTTPS_AVAILABLE,
    ISSUE_UNREACHABLE,
    async_remove_entry,
    async_unload_entry,
)
from custom_components.dahua.const import DOMAIN

ADDRESS = "10.0.0.1"
OTHER = "10.0.0.2"


@pytest.fixture(autouse=True)
def _clean_state():
    for store in (dahua_module._HOST_FAILURES,
                  dahua_module._HOST_UPTIME_STATE,
                  dahua_module._HOST_UPTIME_LOCKS):
        store.clear()
    yield
    for store in (dahua_module._HOST_FAILURES,
                  dahua_module._HOST_UPTIME_STATE,
                  dahua_module._HOST_UPTIME_LOCKS):
        store.clear()


def _entry(hass, *, address=ADDRESS, channel=0):
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Ch%d" % channel,
        unique_id="SERIAL_%d" % channel,
        data={"username": "u", "password": "p", "address": address,
              "port": "80", "rtsp_port": "554", "channel": channel,
              "name": "Ch%d" % channel},
    )
    entry.add_to_hass(hass)
    return entry


def _raise_both(hass, address=ADDRESS):
    for template in (ISSUE_UNREACHABLE, ISSUE_HTTP_DEAD_HTTPS_AVAILABLE):
        ir.async_create_issue(
            hass, DOMAIN, template.format(address),
            is_fixable=False, severity=ir.IssueSeverity.WARNING,
            translation_key="device_unreachable",
            translation_placeholders={"address": address, "entries": "1",
                                      "minutes": "5", "port": "80"})
    dahua_module._HOST_FAILURES[address] = {"consecutive": 7}


def _open_issues(hass, address=ADDRESS):
    registry = ir.async_get(hass)
    return sorted(
        template.format(address)
        for template in (ISSUE_UNREACHABLE, ISSUE_HTTP_DEAD_HTTPS_AVAILABLE)
        if registry.async_get_issue(DOMAIN, template.format(address)) is not None
    )


# --- removing the last entry ------------------------------------------------

async def test_removing_the_last_entry_withdraws_both_issues(hass):
    entry = _entry(hass)
    _raise_both(hass)
    assert len(_open_issues(hass)) == 2

    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()

    assert _open_issues(hass) == [], "a deleted device must not leave its card"


async def test_removing_the_last_entry_forgets_its_failure_count(hass):
    """The count drives the poll backoff, so re-adding the device must not
    inherit a backoff it has not earned."""
    entry = _entry(hass)
    _raise_both(hass)

    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()

    assert ADDRESS not in dahua_module._HOST_FAILURES


# --- but not while the host still has entries -------------------------------

async def test_removing_one_channel_of_a_recorder_keeps_the_issues(hass):
    """An NVR is one entry per channel. Deleting channel 2 must not withdraw a
    card that still describes the host the other ten are on."""
    first = _entry(hass, channel=0)
    _entry(hass, channel=1)
    _raise_both(hass)

    await hass.config_entries.async_remove(first.entry_id)
    await hass.async_block_till_done()

    assert len(_open_issues(hass)) == 2
    assert ADDRESS in dahua_module._HOST_FAILURES


async def test_another_hosts_issues_are_untouched(hass):
    entry = _entry(hass, address=ADDRESS)
    _entry(hass, address=OTHER, channel=1)
    _raise_both(hass, ADDRESS)
    _raise_both(hass, OTHER)

    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()

    assert _open_issues(hass, ADDRESS) == []
    assert len(_open_issues(hass, OTHER)) == 2, "one host's removal is not another's"


# --- and not on an unload, which is also a reload ---------------------------

async def test_unloading_does_not_withdraw_anything(hass):
    """`async_unload_entry` runs on a reload too. Withdrawing there would clear
    the backoff every time somebody saved an option -- and it is also where the
    old, unreachable version of this lived."""
    entry = _entry(hass)
    _raise_both(hass)

    await async_unload_entry(hass, entry)

    assert len(_open_issues(hass)) == 2
    assert ADDRESS in dahua_module._HOST_FAILURES


# --- the hook itself --------------------------------------------------------

async def test_the_hook_runs_even_when_setup_never_succeeded(hass):
    """No coordinator was ever registered, which is *more* likely when a card
    was raised, not less. The old code returned early in that case."""
    entry = _entry(hass)
    _raise_both(hass)
    assert entry.entry_id not in hass.data.get(DOMAIN, {})

    await async_remove_entry(hass, entry)

    assert _open_issues(hass) == []


async def test_an_entry_with_no_address_is_ignored(hass):
    entry = MockConfigEntry(domain=DOMAIN, title="odd", data={})
    entry.add_to_hass(hass)
    _raise_both(hass)

    await async_remove_entry(hass, entry)

    assert len(_open_issues(hass)) == 2, "nothing to forget, so forget nothing"
