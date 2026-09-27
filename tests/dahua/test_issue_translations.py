"""A mistyped translation key ships a card with raw keys on screen.

None of the behavioural tests can see that, because creating an issue does not
resolve its text. These are plain file assertions.
"""

import json
import re
from pathlib import Path

import pytest

TRANSLATIONS = Path(__file__).resolve().parents[2] / "custom_components" / "dahua" / "translations"
EN = json.loads((TRANSLATIONS / "en.json").read_text(encoding="utf-8"))

# What the code actually passes to async_create_issue / async_show_form.
ISSUE_PLACEHOLDERS = {
    "device_unreachable": {"address", "entries", "minutes", "port"},
    "http_dead_https_available": {"address", "entries", "minutes", "port"},
    # Only the title's, for a fixable issue. Its flow fills its own form from
    # the issue's `data`, and a name listed here but not supplied there renders
    # literally as {removed} on screen.
    "siblings_remain": {"address", "count"},
    "removal_broke_things": {"removed", "dependents", "names"},
}
FIX_FLOW_PLACEHOLDERS = {"address", "entries", "port"}
SIBLINGS_FLOW_PLACEHOLDERS = {"address", "count", "titles", "removed",
                              "dependents_note"}


def _placeholders(text: str) -> set:
    return set(re.findall(r"\{(\w+)\}", text))


@pytest.mark.parametrize("key", sorted(ISSUE_PLACEHOLDERS))
def test_every_issue_the_code_raises_has_a_title(key):
    assert EN["issues"][key]["title"].strip()


def test_a_fixable_issue_has_a_fix_flow_and_no_description():
    """hassfest treats description and fix_flow as mutually exclusive.

    An issue is either something you read (title + description) or something
    you act on (title + fix_flow). Supplying both fails validation with
    "two or more values in the same group of exclusion 'fixable'", so the
    explanation has to live in the fix flow's own step.
    """
    issue = EN["issues"]["http_dead_https_available"]
    assert "fix_flow" in issue
    assert "description" not in issue

    step = issue["fix_flow"]["step"]["confirm"]
    assert {"title", "description"} <= set(step)
    assert step["description"].strip()


def test_an_unfixable_issue_has_a_description_and_no_fix_flow():
    issue = EN["issues"]["device_unreachable"]
    assert issue["description"].strip()
    assert "fix_flow" not in issue


def test_the_fix_flow_can_abort():
    abort = EN["issues"]["http_dead_https_available"]["fix_flow"]["abort"]
    assert "not_configured" in abort


@pytest.mark.parametrize("key", sorted(ISSUE_PLACEHOLDERS))
def test_no_text_uses_a_placeholder_the_code_does_not_supply(key):
    """An unsupplied placeholder renders literally as {whatever}."""
    issue = EN["issues"][key]
    used = _placeholders(issue["title"]) | _placeholders(issue.get("description", ""))
    assert used <= ISSUE_PLACEHOLDERS[key], f"unsupplied: {used - ISSUE_PLACEHOLDERS[key]}"


def test_the_siblings_flow_only_uses_its_own_placeholders():
    """The one that caught a real bug: the confirm text named {removed}, which
    async_show_form did not supply, so it would have rendered literally."""
    step = EN["issues"]["siblings_remain"]["fix_flow"]["step"]["confirm"]
    used = _placeholders(step["title"]) | _placeholders(step["description"])
    assert used <= SIBLINGS_FLOW_PLACEHOLDERS, (
        f"unsupplied: {used - SIBLINGS_FLOW_PLACEHOLDERS}")


def test_the_siblings_issue_is_fixable_and_the_notice_is_not():
    """hassfest treats description and fix_flow as mutually exclusive, so an
    issue is either read or acted on -- never both."""
    offer = EN["issues"]["siblings_remain"]
    assert "fix_flow" in offer and "description" not in offer
    assert "not_configured" in offer["fix_flow"]["abort"]

    notice = EN["issues"]["removal_broke_things"]
    assert notice["description"].strip() and "fix_flow" not in notice


def test_the_removal_offer_says_it_cannot_be_undone():
    """It deletes config entries, which is the one irreversible thing any of
    these flows do."""
    text = EN["issues"]["siblings_remain"]["fix_flow"]["step"]["confirm"]["description"]
    assert "cannot be undone" in text.lower()


def test_the_fix_flow_only_uses_its_own_placeholders():
    """Step text is filled from async_show_form, not from the issue's set."""
    step = EN["issues"]["http_dead_https_available"]["fix_flow"]["step"]["confirm"]
    used = _placeholders(step["title"]) | _placeholders(step["description"])
    assert used <= FIX_FLOW_PLACEHOLDERS, f"unsupplied: {used - FIX_FLOW_PLACEHOLDERS}"


def test_adding_issues_did_not_disturb_config_or_options():
    assert "config" in EN and "options" in EN
    assert "scan_interval" in EN["options"]["step"]["user"]["data"]
    assert "reconfigure" in EN["config"]["step"]


def test_the_other_locales_fall_back_to_english():
    """English is the per-key fallback, so copying untranslated text into the
    other eight files would look identical while silently rotting."""
    for path in TRANSLATIONS.glob("*.json"):
        if path.name == "en.json":
            continue
        assert "issues" not in json.loads(path.read_text(encoding="utf-8")), path.name
