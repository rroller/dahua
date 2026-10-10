"""The repair that tidies up after a deletion must not delete a merged recorder.

Reproduced on a live Home Assistant, 2026-09-29, on 1.0.0-beta-7. Ten Dahua config
entries on a DHI-NVR5464, the #827 migration ran, and the log said exactly what it
should:

    192.168.0.213 is now one Dahua entry with 10 channels: 232 entities moved and 9
    redundant entries removed, with all 232 accounted for on the merged entry. Entity
    ids are unchanged, so dashboards and automations keep working

Eighty seconds later that installation had no Dahua config entries, no Dahua devices and
no Dahua entities. 232 entities and their history were gone.

The chain: `_async_merge_host` removes the folded entries, each removal reaches
`async_remove_entry`, which offers to remove whatever entries are left for the address.
The card is `is_persistent=True`, so it outlived the merge, and by then the only entry
left was the merged one holding every channel. `RemoveSiblingsRepairFlow` removes every
entry for the address, so pressing the button deleted the recorder.

**#891 fixed the cause**: the merge now withdraws that card after its own removals, so
there is nothing left to press. This is the other end of the same failure, which #891
does not close. The flow itself will still remove a merged recorder whenever the card
reaches it by another route: a user deleting one channel of a host the migration only
partly merged raises it for real, and an exception between the merge's removals and its
withdrawal leaves it standing, since `_async_merge_host` is called inside a broad
`except`.

So the flow refuses an entry that has subentries. A leftover sibling never has any; an
entry that has them holds every channel of the recorder, and removing it deletes
everything the recorder has.

The guard is deliberately not "owns entities", which was the first thing I tried and is
wrong: the entries this card legitimately removes each own their own channel's entities,
and that is precisely what the user is asking to be rid of.
"""

import pytest
from homeassistant.config_entries import ConfigSubentry
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.dahua.const import DOMAIN
from custom_components.dahua.migrate import CHANNEL_SUBENTRY
from custom_components.dahua.repairs import RemoveSiblingsRepairFlow

ADDRESS = "192.168.0.213"


@pytest.fixture(autouse=True)
def _no_stagger(monkeypatch):
    """The flow staggers its removals so a Dahua web server is not torn down eleven
    times at once. Patched on the module's own constant rather than on
    `asyncio.sleep`, which is global: patching that once made a test count Home
    Assistant's own sleeps as the integration's (#857)."""
    from custom_components.dahua import repairs

    monkeypatch.setattr(repairs, "RELOAD_STAGGER_SECONDS", 0)


def _entry(hass, *, channel=0, title=None):
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=title or f"Ch{channel}",
        unique_id=f"SERIAL_{channel}" if channel else "SERIAL",
        data={
            "username": "u",
            "password": "p",
            "address": ADDRESS,
            "port": "80",
            "rtsp_port": "554",
            "channel": channel,
            "name": title or f"Ch{channel}",
        },
    )
    entry.add_to_hass(hass)
    return entry


def _merged_entry(hass):
    """One entry holding several channels, which is what the migration leaves.

    The subentries are added the way `migrate.py` adds them, through
    `async_add_subentry` with a real `ConfigSubentry`, rather than through a test
    helper's own idea of the shape.
    """
    entry = _entry(hass, channel=0, title="Gerty New")
    for index in (0, 1, 3, 4):
        hass.config_entries.async_add_subentry(
            entry,
            ConfigSubentry(
                data={"channel": index, "name": f"Ch{index}"},
                subentry_type=CHANNEL_SUBENTRY,
                title=f"Ch{index}",
                unique_id=f"{ADDRESS}-{index}",
            ),
        )
    assert entry.subentries, "the merged entry must really have subentries"
    return entry


def _flow(hass):
    flow = RemoveSiblingsRepairFlow({"address": ADDRESS, "removed": "Ch2"})
    flow.hass = hass
    return flow


async def test_the_flow_will_not_remove_a_merged_recorder(hass):
    """The regression test, and what actually happened: a persistent card, a merged
    entry, one press of the button, 232 entities gone."""
    merged = _merged_entry(hass)

    await _flow(hass).async_step_confirm({})
    await hass.async_block_till_done()

    assert (
        hass.config_entries.async_get_entry(merged.entry_id) is not None
    ), "the merged recorder was deleted by the repair meant to tidy up after it"


async def test_the_flow_still_removes_entries_that_were_never_merged(hass):
    """Unchanged for the case it exists for. An NVR the migration refused really does
    have an entry per channel, and finishing the deletion by hand is eleven clicks.
    These entries own their own channel's entities, which is why the guard cannot be
    "owns entities"."""
    ones = [_entry(hass, channel=index) for index in (1, 3, 4)]

    await _flow(hass).async_step_confirm({})
    await hass.async_block_till_done()

    for entry in ones:
        assert hass.config_entries.async_get_entry(entry.entry_id) is None


async def test_a_merged_entry_is_kept_while_its_unmerged_siblings_go(hass):
    """Both at once, which is what a partly refused migration leaves. The leftovers
    are still removed, so the card does something, and the recorder survives."""
    merged = _merged_entry(hass)
    stray = _entry(hass, channel=9)

    await _flow(hass).async_step_confirm({})
    await hass.async_block_till_done()

    assert hass.config_entries.async_get_entry(merged.entry_id) is not None
    assert hass.config_entries.async_get_entry(stray.entry_id) is None


async def test_a_camera_that_holds_one_channel_is_a_leftover_however_it_is_shaped(hass):
    """The case the guard's wording changed for.

    It used to ask `if entry.subentries`, which was a proxy for "holds many
    channels". The add flow gave even a single camera a subentry of its own for a
    while, so a camera added in that window read as a merged recorder and was
    protected from a card the user had explicitly asked for -- while the same
    camera added before or after it was removed. Counting the channels says what
    the guard has always meant, and gives all three the same answer.
    """
    one_subentry = _entry(hass, channel=7, title="Front Door")
    hass.config_entries.async_add_subentry(
        one_subentry,
        ConfigSubentry(
            data={"channel": 7, "name": "Front Door"},
            subentry_type=CHANNEL_SUBENTRY,
            title="Front Door",
            unique_id=f"{ADDRESS}-7",
        ),
    )
    assert one_subentry.subentries, "the shape under test needs its subentry"

    flat = _entry(hass, channel=8, title="Side Gate")
    assert not flat.subentries

    await _flow(hass).async_step_confirm({})
    await hass.async_block_till_done()

    assert (
        hass.config_entries.async_get_entry(one_subentry.entry_id) is None
    ), "a one channel entry was protected because of how it was shaped"
    assert hass.config_entries.async_get_entry(flat.entry_id) is None


async def test_the_form_still_does_nothing_until_the_button_is_pressed(hass):
    """Opening the card must remain harmless. That is the only reason this was
    recoverable at all: the registry copies the merge writes before it touches
    anything were still the pre-merge state when the entries went.

    This used to assert a form was shown. It is an abort now, and deliberately:
    the only entry here holds the whole recorder, so there is nothing this card
    can remove, and offering a form that promises to remove zero entries is how
    it came to claim success for doing nothing. The assertion that matters --
    that opening it changes nothing -- is unchanged and is what the docstring
    above is about.
    """
    merged = _merged_entry(hass)

    result = await _flow(hass).async_step_confirm()

    assert result["type"] == "abort"
    assert result["reason"] == "nothing_to_remove"
    assert hass.config_entries.async_get_entry(merged.entry_id) is not None


# --- the form has to say what the button will do ----------------------------


async def test_the_form_names_only_the_entries_that_will_go(hass):
    """The form counted and listed every entry at the address while the removal
    loop skipped the merged one. So the card said "This removes the remaining 3
    for you" and named an entry it would not touch, for the one irreversible
    action in the file."""
    _merged_entry(hass)
    _entry(hass, channel=5, title="Leftover")

    shown = await _flow(hass).async_step_confirm()

    placeholders = shown["description_placeholders"]
    assert placeholders["count"] == "1", "the count included the protected entry"
    assert placeholders["titles"] == "Leftover"
    assert "Gerty New" not in placeholders["titles"]


async def test_the_form_says_what_is_being_left_alone(hass):
    """Naming it only in the log left the user to work out why the count did not
    match what they could see on the integrations page."""
    _merged_entry(hass)
    _entry(hass, channel=5, title="Leftover")

    shown = await _flow(hass).async_step_confirm()

    note = shown["description_placeholders"]["protected_note"]
    assert "Gerty New" in note
    assert "integrations page" in note


async def test_nothing_protected_means_no_note_at_all(hass):
    """A whole sentence or nothing, the same rule dependents_note follows."""
    _entry(hass, channel=5, title="Leftover")

    shown = await _flow(hass).async_step_confirm()

    assert shown["description_placeholders"]["protected_note"] == ""


# --- and must not claim to have done it ------------------------------------


async def test_a_card_that_can_remove_nothing_says_so(hass):
    """Where every entry left is a merged recorder, the old flow removed
    nothing, called async_create_entry -- which Home Assistant renders as the
    repair having been fixed -- and left the only explanation in the log. The
    user is told the recorder is gone when it is not."""
    merged = _merged_entry(hass)

    result = await _flow(hass).async_step_confirm({})

    assert result["type"] == "abort"
    assert result["reason"] == "nothing_to_remove"
    assert hass.config_entries.async_get_entry(merged.entry_id) is not None


async def test_a_card_that_can_remove_nothing_is_withdrawn(hass):
    """It cannot become true later: the entry is protected for as long as it
    holds the whole recorder, so leaving the card up offers the same dead end
    again after every restart."""
    from homeassistant.helpers import issue_registry as ir

    from custom_components.dahua import ISSUE_SIBLINGS_REMAIN

    _merged_entry(hass)
    issue_id = ISSUE_SIBLINGS_REMAIN.format(ADDRESS)
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=True,
        severity=ir.IssueSeverity.WARNING,
        translation_key="siblings_remain",
    )

    await _flow(hass).async_step_confirm({})

    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None
