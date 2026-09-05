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
}
FIX_FLOW_PLACEHOLDERS = {"address", "entries"}


def _placeholders(text: str) -> set:
    return set(re.findall(r"\{(\w+)\}", text))


@pytest.mark.parametrize("key", ["device_unreachable", "http_dead_https_available"])
def test_every_issue_the_code_raises_has_english_text(key):
    assert {"title", "description"} <= set(EN["issues"][key])
    assert EN["issues"][key]["title"].strip()
    assert EN["issues"][key]["description"].strip()


def test_the_fixable_issue_has_a_fix_flow_step():
    """is_fixable=True with no fix_flow gives a card whose button goes nowhere."""
    step = EN["issues"]["http_dead_https_available"]["fix_flow"]["step"]["confirm"]
    assert {"title", "description"} <= set(step)


def test_the_fix_flow_can_abort():
    abort = EN["issues"]["http_dead_https_available"]["fix_flow"]["abort"]
    assert "not_configured" in abort


def test_the_unfixable_issue_has_no_fix_flow():
    """A fix_flow on is_fixable=False would offer a button that does nothing."""
    assert "fix_flow" not in EN["issues"]["device_unreachable"]


@pytest.mark.parametrize("key", ["device_unreachable", "http_dead_https_available"])
def test_no_text_uses_a_placeholder_the_code_does_not_supply(key):
    """An unsupplied placeholder renders literally as {whatever}."""
    used = _placeholders(EN["issues"][key]["title"]) | _placeholders(
        EN["issues"][key]["description"]
    )
    assert used <= ISSUE_PLACEHOLDERS[key], f"unsupplied: {used - ISSUE_PLACEHOLDERS[key]}"


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
