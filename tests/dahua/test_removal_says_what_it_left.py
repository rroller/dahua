"""Removing one channel of a recorder should say the others are still there.

An NVR is added as one config entry per channel, so removing "the recorder" is as
many deletions as it has channels -- eleven, on the setup this was written
against -- and nobody notices until they are part-way through. Home Assistant
gives no warning, because it has no idea the entries are related.

It also cannot be warned about *before* the fact. `ConfigEntries._async_remove`
unloads the entry, deletes it from its own registry, and only then calls an
integration's `async_remove_entry`; that hook's return value is never read and its
exceptions are only logged. There is no pre-removal hook, no veto, and no way to
put a form in front of the user. So the offer arrives just after the first
deletion rather than just before it, which is the closest thing available.

The same hook is the only place that can say what *referenced* the entry, because
it runs while the entity and device registry rows still exist -- Home Assistant
clears them immediately afterwards.

**At most one card per removal.** Two would be noise on a single deletion, so the
sibling offer carries the dependents note when both apply.
"""

import pytest
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components import dahua as dahua_module
from custom_components.dahua import (
    ISSUE_REMOVAL_BROKE_THINGS,
    ISSUE_SIBLINGS_REMAIN,
    _async_dependents,
    _describe_dependents,
    async_remove_entry,
)
from custom_components.dahua.const import DOMAIN

ADDRESS = "10.0.0.1"
OTHER = "10.0.0.2"


@pytest.fixture(autouse=True)
def _clean_state():
    for store in (
        dahua_module._HOST_FAILURES,
        dahua_module._HOST_UPTIME_STATE,
        dahua_module._HOST_UPTIME_LOCKS,
    ):
        store.clear()
    yield
    for store in (
        dahua_module._HOST_FAILURES,
        dahua_module._HOST_UPTIME_STATE,
        dahua_module._HOST_UPTIME_LOCKS,
    ):
        store.clear()


@pytest.fixture(autouse=True)
def _no_dependents(monkeypatch):
    """Most tests here are about the sibling half, so the search side is stubbed
    out and they do not depend on whether the search component is loaded."""
    monkeypatch.setattr(dahua_module, "_async_dependents", lambda hass, entry_id: {})


def _entry(hass, *, address=ADDRESS, channel=0, title=None, add=True):
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=title or "Ch%d" % channel,
        unique_id="SERIAL_%d_%s" % (channel, address),
        data={
            "username": "u",
            "password": "p",
            "address": address,
            "port": "80",
            "rtsp_port": "554",
            "channel": channel,
            "name": "Ch%d" % channel,
        },
    )
    if add:
        entry.add_to_hass(hass)
    return entry


def _issue(hass, issue_id):
    return ir.async_get(hass).async_get_issue(DOMAIN, issue_id)


# --- the offer --------------------------------------------------------------


async def test_removing_one_channel_offers_to_remove_the_rest(hass):
    gone = _entry(hass, channel=0, title="Front", add=False)
    _entry(hass, channel=1)
    _entry(hass, channel=2)

    await async_remove_entry(hass, gone)

    issue = _issue(hass, ISSUE_SIBLINGS_REMAIN.format(ADDRESS))
    assert issue is not None, "eleven deletions with no hint is the whole problem"
    assert issue.is_fixable, "it has to be actionable, not just a notice"
    assert issue.translation_placeholders["count"] == "2"


async def test_the_offer_survives_a_restart(hass):
    """A removal is usually followed by a restart, and a card that vanishes over
    one is no use to somebody who wanted to read it afterwards."""
    gone = _entry(hass, channel=0, add=False)
    _entry(hass, channel=1)

    await async_remove_entry(hass, gone)

    assert _issue(hass, ISSUE_SIBLINGS_REMAIN.format(ADDRESS)).is_persistent


async def test_the_offer_carries_what_the_flow_needs(hass):
    """The flow fills its own form from this, not from the placeholders."""
    gone = _entry(hass, channel=0, title="Front Door", add=False)
    _entry(hass, channel=1)

    await async_remove_entry(hass, gone)

    data = _issue(hass, ISSUE_SIBLINGS_REMAIN.format(ADDRESS)).data
    assert data["address"] == ADDRESS
    assert data["removed"] == "Front Door"
    assert data["dependents_note"] == ""


async def test_removing_the_last_entry_offers_nothing(hass):
    """Nothing left to remove, so nothing to say. The common case is silent."""
    gone = _entry(hass, channel=0, add=False)

    await async_remove_entry(hass, gone)

    assert _issue(hass, ISSUE_SIBLINGS_REMAIN.format(ADDRESS)) is None


async def test_another_hosts_entries_are_not_counted(hass):
    gone = _entry(hass, address=ADDRESS, channel=0, add=False)
    _entry(hass, address=OTHER, channel=1)

    await async_remove_entry(hass, gone)

    assert (
        _issue(hass, ISSUE_SIBLINGS_REMAIN.format(ADDRESS)) is None
    ), "another recorder's entries are not this one's siblings"


# --- the notice -------------------------------------------------------------


async def test_a_removal_that_broke_automations_says_so(hass, monkeypatch):
    monkeypatch.setattr(
        dahua_module,
        "_async_dependents",
        lambda hass_, entry_id: {"automation": ["automation.porch"]},
    )
    gone = _entry(hass, channel=0, title="Front", add=False)

    await async_remove_entry(hass, gone)

    issue = _issue(hass, ISSUE_REMOVAL_BROKE_THINGS.format(gone.entry_id))
    assert issue is not None
    assert not issue.is_fixable, "deleting somebody else's automation is not ours to do"
    assert issue.translation_placeholders["dependents"] == "1 automation"
    assert "automation.porch" in issue.translation_placeholders["names"]


async def test_only_one_card_when_both_apply(hass, monkeypatch):
    """Two cards on one deletion is noise. The offer carries the note instead."""
    monkeypatch.setattr(
        dahua_module,
        "_async_dependents",
        lambda hass_, entry_id: {"script": ["script.chime"]},
    )
    gone = _entry(hass, channel=0, add=False)
    _entry(hass, channel=1)

    await async_remove_entry(hass, gone)

    offer = _issue(hass, ISSUE_SIBLINGS_REMAIN.format(ADDRESS))
    assert offer is not None
    assert _issue(hass, ISSUE_REMOVAL_BROKE_THINGS.format(gone.entry_id)) is None
    assert "1 script" in offer.data["dependents_note"]


async def test_nothing_referenced_it_and_nothing_remains_is_silent(hass):
    gone = _entry(hass, channel=0, add=False)

    await async_remove_entry(hass, gone)

    assert _issue(hass, ISSUE_REMOVAL_BROKE_THINGS.format(gone.entry_id)) is None


# --- the wording ------------------------------------------------------------


async def test_working_out_what_referenced_it_never_fails_the_removal(
    hass, monkeypatch
):
    """The search component is an `after_dependency`: available in practice, not
    something to fail on. And this runs *after* Home Assistant has already deleted the
    entry, so raising here cannot undo anything, it only loses the note.

    `async_remove_entry`'s exceptions are logged and discarded, so a failure here would
    be invisible except as a card that never appeared.
    """

    def _explodes(*args, **kwargs):
        raise RuntimeError("the search component moved")

    monkeypatch.setattr(
        "homeassistant.helpers.entity.entity_sources", _explodes, raising=False
    )

    assert _async_dependents(hass, "any-entry-id") == {}


async def test_a_working_search_is_filtered_to_what_breaks_silently(hass, monkeypatch):
    """The control, and the filtering.

    Without this the test above would pass for a function that always returns nothing,
    which is exactly the failure it is meant to rule out. It also pins which kinds are
    reported: a dashboard card pointing at a missing entity says so on screen, an
    automation just stops firing, so only the silent ones are listed.

    Note that the guard tested above covers the search *call*. The filtering below it
    sits outside the try, so this exercises a different few lines.
    """

    class _Searcher:
        def __init__(self, hass, sources):
            pass

        def async_search(self, item_type, entry_id):
            return {
                "automation": {"automation.gate"},
                "script": {"script.arm"},
                "scene": {"scene.night"},
                "group": {"group.cameras"},
                "config_entry": {"something"},
                "area": {"area.garden"},
            }

    monkeypatch.setattr(
        "homeassistant.components.search.Searcher", _Searcher, raising=False
    )

    answer = _async_dependents(hass, "an-entry")

    assert answer == {
        "automation": ["automation.gate"],
        "script": ["script.arm"],
        "scene": ["scene.night"],
        "group": ["group.cameras"],
    }, answer


async def test_a_search_that_found_nothing_reports_nothing(hass, monkeypatch):
    """An empty answer must not become a card saying something broke."""

    class _Searcher:
        def __init__(self, hass, sources):
            pass

        def async_search(self, item_type, entry_id):
            return {"automation": set()}

    monkeypatch.setattr(
        "homeassistant.components.search.Searcher", _Searcher, raising=False
    )

    assert _async_dependents(hass, "an-entry") == {}


def test_one_of_a_kind_is_singular():
    assert _describe_dependents({"automation": ["a"]}) == "1 automation"


def test_several_of_a_kind_are_plural():
    assert _describe_dependents({"script": ["a", "b"]}) == "2 scripts"


def test_two_kinds_are_joined_with_and():
    assert (
        _describe_dependents({"automation": ["a"], "script": ["b", "c"]})
        == "1 automation and 2 scripts"
    )


def test_three_kinds_use_commas_then_and():
    assert (
        _describe_dependents({"automation": ["a"], "script": ["b"], "scene": ["c"]})
        == "1 automation, 1 script and 1 scene"
    )


def test_nothing_describes_as_nothing():
    assert _describe_dependents({}) == ""
