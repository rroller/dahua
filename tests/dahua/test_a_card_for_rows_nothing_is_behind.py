"""Leftover registry rows, offered together instead of one at a time.

Asked for by @kmich on #1103: the `disarming` and `event_notifications`
switches an earlier release created on a VTH are gated off now, so they moved
through the serial migration and then sat unavailable with no way to be rid of
them but by hand, twenty times over three monitors.

**This is the shape that deleted a recorder.** The #827 merge card was
persistent, outlived the evidence it was raised on, and destroyed 232 entities
when somebody pressed it. So the bias here is towards doing nothing:

* nothing is offered unless every channel is initialized, polling cleanly, and
  has no capability probe still unanswered. A channel that failed its first
  refresh (#1085) has every row unavailable, and a probe that has not answered
  (#1057) has an entity still to come; both look exactly like leftovers.
* the card is not persistent, and carries no list. The #827 card carried its
  evidence and then acted on it long after it stopped being true.
* the flow recomputes at the moment the button is pressed, and removes only
  what is still missing then.
* a disabled row is never a leftover. There is no entity behind it because the
  user said so.

A row with no live entity is one with **no state object, or a state Home
Assistant marked `restored`**. Which of the two depends only on timing:
`_write_unavailable_states` runs on EVENT_HOMEASSISTANT_START and only when
Home Assistant was not already running, so before it a leftover has no state
and after it has a restored one.
"""

import pytest
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.dahua import ISSUE_LEFTOVER_ROWS, migrate
from custom_components.dahua.repairs import RemoveLeftoverRowsRepairFlow

DOMAIN = "dahua"


class _Coordinator:
    """What the health gate reads, and nothing else."""

    def __init__(self, initialized=True, success=True, unanswered=()):
        self.initialized = initialized
        self.last_update_success = success
        self._unanswered = set(unanswered)

    def probes_that_did_not_answer(self):
        return self._unanswered


async def _entry_with_rows(hass, live=("switch.live",), leftover=("switch.gone",)):
    entry = MockConfigEntry(domain=DOMAIN, data={"address": "10.0.0.230"})
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    made = {}
    for entity_id in list(live) + list(leftover):
        suffix = entity_id.split(".")[1]
        row = registry.async_get_or_create(
            "switch",
            DOMAIN,
            "SER_" + suffix,
            config_entry=entry,
            suggested_object_id=suffix,
        )
        made[entity_id] = row
    for entity_id in live:
        hass.states.async_set(made[entity_id].entity_id, "off")
    for entity_id in leftover:
        # Exactly what Home Assistant writes for a row with no entity. Left
        # with no state at all in some of the tests below, because both shapes
        # occur and both mean the same thing.
        hass.states.async_set(
            made[entity_id].entity_id, "unavailable", {"restored": True}
        )
    return entry, made


def _issue(hass, entry):
    return ir.async_get(hass).async_get_issue(
        DOMAIN, ISSUE_LEFTOVER_ROWS.format(entry.entry_id)
    )


# --- what counts as a leftover ------------------------------------------------


async def test_a_restored_row_is_a_leftover(hass):
    entry, made = await _entry_with_rows(hass)

    assert migrate.leftover_rows(hass, entry) == [made["switch.gone"].entity_id]


async def test_a_row_with_no_state_at_all_is_one_too(hass):
    """Before Home Assistant writes the unavailable states, which it does at
    start and only when it was not already running. Reading `restored` alone
    would have made the answer depend on listener order."""
    entry, made = await _entry_with_rows(hass, leftover=())
    registry = er.async_get(hass)
    row = registry.async_get_or_create(
        "switch", DOMAIN, "SER_never", config_entry=entry, suggested_object_id="never"
    )

    assert row.entity_id in migrate.leftover_rows(hass, entry)


async def test_a_live_entity_is_not_a_leftover(hass):
    entry, made = await _entry_with_rows(hass, leftover=())

    assert migrate.leftover_rows(hass, entry) == []


async def test_a_disabled_row_is_never_a_leftover(hass):
    """The user disabled it. There is no entity behind it on purpose, and
    deleting it would be acting on their own instruction as though it were a
    fault."""
    entry, made = await _entry_with_rows(hass, leftover=())
    registry = er.async_get(hass)
    row = registry.async_get_or_create(
        "switch",
        DOMAIN,
        "SER_off",
        config_entry=entry,
        suggested_object_id="off",
        disabled_by=er.RegistryEntryDisabler.USER,
    )

    assert row.entity_id not in migrate.leftover_rows(hass, entry)


async def test_another_entry_s_rows_are_not_this_one_s(hass):
    entry, made = await _entry_with_rows(hass)
    other = MockConfigEntry(domain=DOMAIN, data={"address": "10.0.0.231"})
    other.add_to_hass(hass)
    registry = er.async_get(hass)
    stranger = registry.async_get_or_create(
        "switch", DOMAIN, "OTHER_gone", config_entry=other, suggested_object_id="far"
    )

    assert stranger.entity_id not in migrate.leftover_rows(hass, entry)


# --- when the card is raised at all -------------------------------------------


async def test_a_healthy_entry_with_leftovers_gets_the_card(hass):
    entry, _ = await _entry_with_rows(hass)

    found = await migrate.async_offer_leftover_rows(hass, entry, [_Coordinator()])

    assert found == 1
    issue = _issue(hass, entry)
    assert issue is not None
    assert (
        issue.is_persistent is False
    ), "persistence is what let the #827 card act late"
    assert issue.translation_placeholders["count"] == "1"


async def test_a_failed_channel_is_not_offered_as_leftovers(hass):
    """#1085's recovery path. Every row of a channel that failed its first
    refresh is unavailable until it comes back, so the card would have offered
    to delete the lot."""
    entry, _ = await _entry_with_rows(hass)

    found = await migrate.async_offer_leftover_rows(
        hass, entry, [_Coordinator(), _Coordinator(success=False)]
    )

    assert found == 0
    assert _issue(hass, entry) is None


async def test_a_channel_that_never_initialized_is_not_offered(hass):
    entry, _ = await _entry_with_rows(hass)

    found = await migrate.async_offer_leftover_rows(
        hass, entry, [_Coordinator(initialized=False)]
    )

    assert found == 0
    assert _issue(hass, entry) is None


async def test_an_unanswered_capability_probe_holds_the_card_back(hass):
    """#1057: the entity is built when the retry finally gets an answer, so
    until then its row is indistinguishable from a leftover."""
    entry, _ = await _entry_with_rows(hass)

    found = await migrate.async_offer_leftover_rows(
        hass, entry, [_Coordinator(unanswered={"coaxial_control"})]
    )

    assert found == 0
    assert _issue(hass, entry) is None


async def test_no_coordinators_means_no_opinion(hass):
    entry, _ = await _entry_with_rows(hass)

    assert await migrate.async_offer_leftover_rows(hass, entry, []) == 0
    assert _issue(hass, entry) is None


async def test_nothing_left_over_raises_nothing(hass):
    entry, _ = await _entry_with_rows(hass, leftover=())

    assert await migrate.async_offer_leftover_rows(hass, entry, [_Coordinator()]) == 0
    assert _issue(hass, entry) is None


async def test_a_card_is_cleared_once_the_rows_come_back(hass):
    """Recomputed every start rather than carried. An entity restored by a
    capability retry, or by the user re-enabling a platform, must take its card
    with it."""
    entry, made = await _entry_with_rows(hass)
    await migrate.async_offer_leftover_rows(hass, entry, [_Coordinator()])
    assert _issue(hass, entry) is not None

    hass.states.async_set(made["switch.gone"].entity_id, "off")
    await migrate.async_offer_leftover_rows(hass, entry, [_Coordinator()])

    assert _issue(hass, entry) is None


async def test_an_unhealthy_entry_clears_a_card_raised_earlier(hass):
    """A camera that goes offline after the card was raised must not leave a
    card standing that now describes its whole entity set."""
    entry, _ = await _entry_with_rows(hass)
    await migrate.async_offer_leftover_rows(hass, entry, [_Coordinator()])

    await migrate.async_offer_leftover_rows(hass, entry, [_Coordinator(success=False)])

    assert _issue(hass, entry) is None


# --- and what the flow does ---------------------------------------------------


def _flow(hass, entry):
    flow = RemoveLeftoverRowsRepairFlow({"entry_id": entry.entry_id})
    flow.hass = hass
    return flow


async def test_the_form_names_every_row_it_would_remove(hass):
    entry, made = await _entry_with_rows(hass)
    await migrate.async_offer_leftover_rows(hass, entry, [_Coordinator()])

    result = await _flow(hass, entry).async_step_init()

    assert result["type"] == "form"
    assert (
        made["switch.gone"].entity_id in result["description_placeholders"]["entities"]
    )
    assert made["switch.live"].entity_id not in (
        result["description_placeholders"]["entities"]
    )


async def test_confirming_removes_only_the_leftovers(hass):
    entry, made = await _entry_with_rows(hass)
    await migrate.async_offer_leftover_rows(hass, entry, [_Coordinator()])
    flow = _flow(hass, entry)
    await flow.async_step_init()

    result = await flow.async_step_confirm({})

    registry = er.async_get(hass)
    assert result["type"] == "create_entry"
    assert registry.async_get(made["switch.gone"].entity_id) is None
    assert registry.async_get(made["switch.live"].entity_id) is not None
    assert _issue(hass, entry) is None


async def test_a_row_that_came_back_before_the_button_is_not_removed(hass):
    """The reason the list is recomputed in the flow rather than carried in the
    card. A capability retry or a reload can restore an entity between the card
    being raised and somebody pressing it, and the #827 card acted on evidence
    that had gone stale exactly this way."""
    entry, made = await _entry_with_rows(hass)
    await migrate.async_offer_leftover_rows(hass, entry, [_Coordinator()])
    flow = _flow(hass, entry)
    await flow.async_step_init()

    hass.states.async_set(made["switch.gone"].entity_id, "off")
    result = await flow.async_step_confirm({})

    assert result["type"] == "abort"
    assert result["reason"] == "nothing_to_remove"
    assert er.async_get(hass).async_get(made["switch.gone"].entity_id) is not None


async def test_a_removed_config_entry_aborts_rather_than_raising(hass):
    entry, _ = await _entry_with_rows(hass)
    flow = RemoveLeftoverRowsRepairFlow({"entry_id": "no-such-entry"})
    flow.hass = hass

    result = await flow.async_step_init()

    assert result["type"] == "abort"
    assert result["reason"] == "not_configured"


async def test_opening_the_card_removes_nothing(hass):
    """Nothing happens until the button is pressed, which is the rule the one
    irreversible action in this file has to follow."""
    entry, made = await _entry_with_rows(hass)
    await migrate.async_offer_leftover_rows(hass, entry, [_Coordinator()])

    await _flow(hass, entry).async_step_init()

    assert er.async_get(hass).async_get(made["switch.gone"].entity_id) is not None


async def test_one_row_deleted_by_hand_does_not_stop_the_rest(hass):
    """Somebody deleting one from its own settings page while the card is open
    is ordinary, and must not leave the others behind.

    The `async_get` guard in the loop is not reachable from here, and is not
    claimed to be: the list is recomputed inside the step with no await before
    the removals, so a row cannot vanish in between. It is there because
    `async_remove` raises on an unknown id, and a half-finished removal is the
    worst outcome this flow has.
    """
    entry, made = await _entry_with_rows(
        hass, leftover=("switch.gone", "switch.also_gone")
    )
    await migrate.async_offer_leftover_rows(hass, entry, [_Coordinator()])
    flow = _flow(hass, entry)
    await flow.async_step_init()
    registry = er.async_get(hass)
    registry.async_remove(made["switch.gone"].entity_id)

    result = await flow.async_step_confirm({})

    assert result["type"] == "create_entry"
    assert registry.async_get(made["switch.also_gone"].entity_id) is None
    assert registry.async_get(made["switch.live"].entity_id) is not None
