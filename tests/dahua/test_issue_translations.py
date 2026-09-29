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
    "channel_not_added": {"address", "channel", "reason"},
}
FIX_FLOW_PLACEHOLDERS = {"address", "entries", "port"}
SIBLINGS_FLOW_PLACEHOLDERS = {"address", "count", "titles", "removed",
                              "dependents_note"}


def _placeholders(text: str) -> set:
    return set(re.findall(r"\{(\w+)\}", text))


# How the code says "raise this issue". The lookbehind keeps an entity's
# `_attr_translation_key` out of it, which matters now that the scan below covers
# every module rather than the three that happened to raise an issue when it was
# written. Compiled once here so the control test exercises this pattern and not a
# copy of it.
ISSUE_KEY = re.compile(r'(?<!\w)translation_key="([a-z_]+)"')


def test_the_map_above_covers_every_issue_the_code_raises():
    """ISSUE_PLACEHOLDERS is hand written, and everything else in this file is
    parametrized over it, so an issue missing from it is not checked at all.

    That is a silent gap rather than a failure, which is the worst kind: adding
    channel_not_added left it untested here and every test in this file still passed.
    Deriving the set from the source makes the drift impossible instead.
    """
    # Every module, not the three that happened to raise an issue when this
    # was written. Two of them moved to host.py and this reported them as
    # being in the map but not the code, which is exactly backwards.
    from .integration_source import source as integration_source

    source = integration_source()

    raised = set(ISSUE_KEY.findall(source))

    assert raised == set(ISSUE_PLACEHOLDERS), (
        "in the code but not the map: %s; in the map but not the code: %s"
        % (sorted(raised - set(ISSUE_PLACEHOLDERS)),
           sorted(set(ISSUE_PLACEHOLDERS) - raised)))


def test_the_pattern_does_not_collect_an_entitys_translation_key():
    """`_attr_translation_key` is a different thing that reads the same.

    The lookbehind is what keeps them apart, and without a control it is a
    character nobody would miss if it went. Entities happen to write theirs with
    spaces around the `=`, so they slip past by luck rather than by design, and
    widening the scan above from three files to the whole package is what brought
    them within reach of this pattern in the first place.
    """
    sample = (
        'ir.async_create_issue(hass, DOMAIN, key, translation_key="device_unreachable")\n'
        '_attr_translation_key="firmware_version"\n'
        '    _attr_translation_key = "serial_number"\n'
    )

    assert set(ISSUE_KEY.findall(sample)) == {"device_unreachable"}
    assert "firmware_version" in re.findall(r'translation_key="([a-z_]+)"', sample), \
        "the sample no longer reproduces what the lookbehind is for"


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
