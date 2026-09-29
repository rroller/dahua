"""Forty five event sensors, named from the translation file, with nothing moved.

This is the largest group of names in the integration and the one with a catch: the
unique id is derived from the display name, so the name feeds entity identity. The
translation key is therefore the *same slug* as the id suffix, which is what makes
this change free of migration. Change the slug and you rename the entity.

Three things have to agree, and none of them is checked by anything else:

  the codes the platform can produce
  TRANSLATED_EVENTS
  the binary_sensor section of translations/en.json

A code missing from the file is not an error at runtime. It falls back to the
derived English name, which is the right thing to do for a hand edited .storage and
the wrong thing to ship, because it looks perfectly normal in English and cannot be
translated. So the fallback is tested for existing, and then tested for being
unreachable by any code the integration actually produces.

The derivation is called, not reimplemented. A test that rewrote that regex would
agree with itself whatever the code did.
"""

import ast
import io
import json
import re
from pathlib import Path

from custom_components.dahua.binary_sensor import (
    NAME_OVERRIDES,
    TRANSLATED_EVENTS,
    event_display_name,
    event_translation_key,
)
from custom_components.dahua.config_flow import ALL_EVENTS

PACKAGE = Path(__file__).resolve().parents[2] / "custom_components" / "dahua"

# The four the platform adds for a doorbell whether or not they were selected.
# Read out of binary_sensor.py rather than repeated, so adding a fifth fails here.
def _doorbell_codes():
    source = io.open(PACKAGE / "binary_sensor.py", encoding="utf-8").read()
    setup = next(node for node in ast.parse(source).body
                 if isinstance(node, ast.AsyncFunctionDef)
                 and node.name == "async_setup_entry")
    codes = []
    for node in ast.walk(setup):
        if (isinstance(node, ast.Call)
                and getattr(node.func, "id", None) == "DahuaEventSensor"):
            for argument in node.args:
                if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                    codes.append(argument.value)
    return codes


def _producible():
    """Every code the platform can build a sensor for."""
    return {code for code in ALL_EVENTS if code != "All"} | set(_doorbell_codes())


def _names_in_file():
    """slug -> the English name, not slug -> {"name": ...}.

    The first version of this returned the wrapper dicts, so every comparison
    against a derived string failed. It was the run that caught it, not the review.
    """
    data = json.load(io.open(PACKAGE / "translations" / "en.json", encoding="utf-8"))
    return {slug: entry["name"]
            for slug, entry in data["entity"]["binary_sensor"].items()}


# --- the three sides agree ---------------------------------------------------

def test_every_code_the_platform_can_produce_has_a_string():
    """The fallback exists for a config nobody else wrote. Reaching it by adding a
    code to ALL_EVENTS and forgetting the string would ship an entity that reads
    correctly in English and cannot be translated at all, which is the failure this
    whole change is about."""
    missing = sorted(_producible() - set(TRANSLATED_EVENTS))

    assert not missing, "produced by the platform, no string: %s" % missing


def test_translated_events_matches_the_file():
    """Both directions. A code in the set with no string falls back silently, and a
    string with no code is one a translator spends time on for nothing."""
    keys_in_file = set(_names_in_file()) - {"authorized_vehicle"}
    keys_from_codes = {event_translation_key(code) for code in TRANSLATED_EVENTS}

    assert keys_from_codes == keys_in_file, (
        "in the set but not the file: %s; in the file but not the set: %s"
        % (sorted(keys_from_codes - keys_in_file),
           sorted(keys_in_file - keys_from_codes)))


def test_each_string_is_what_the_code_derived():
    """The names are carried across unchanged, so nobody's sensor is renamed."""
    names = _names_in_file()
    wrong = {code: (event_display_name(code), names.get(event_translation_key(code)))
             for code in sorted(TRANSLATED_EVENTS)
             if names.get(event_translation_key(code)) != event_display_name(code)}

    assert not wrong, "the file disagrees with the derivation: %s" % wrong


# --- and the slug is the identity, which is why nothing moved ----------------

def test_the_translation_key_is_the_unique_id_suffix():
    """Not a coincidence worth losing. If these ever diverge, the key stops being
    free and every existing sensor needs migrating."""
    for code in sorted(TRANSLATED_EVENTS):
        expected = event_display_name(code).lower().replace(" ", "_")
        assert event_translation_key(code) == expected, code


def test_no_two_codes_share_a_slug():
    """They would share an entity id as well as a name, and one would win."""
    slugs = [event_translation_key(code) for code in TRANSLATED_EVENTS]

    assert len(set(slugs)) == len(slugs), "collision among %s" % sorted(slugs)


def test_every_slug_is_a_valid_translation_key():
    """Home Assistant's own validator wants a slug. A key with a capital or a space
    is not an error: the lookup simply never matches."""
    bad = [(code, event_translation_key(code)) for code in sorted(TRANSLATED_EVENTS)
           if not re.fullmatch(r"[a-z0-9_]+", event_translation_key(code))]

    assert not bad, "not slugs: %s" % bad


# --- the derivation itself ---------------------------------------------------

def test_the_three_hand_written_names_are_the_overrides():
    """These three are not what the regex would produce, which is the point of
    them, so they are the ones a refactor of it would quietly change."""
    assert event_display_name("VideoMotion") == "Motion Alarm"
    assert event_display_name("CrossLineDetection") == "Cross Line Alarm"
    assert event_display_name("DoorbellPressed") == "Button Pressed"
    assert set(NAME_OVERRIDES) == {"VideoMotion", "CrossLineDetection",
                                   "DoorbellPressed"}


def test_the_regex_splits_on_a_capital_after_a_lower_case_letter_only():
    """So IVS stays IVS rather than becoming I V S. The two awkward results are
    kept deliberately: NTPAdjustTime and MDResult read badly in English, and
    renaming them is a separate decision from making them translatable. A
    translator can already write something better in their own language."""
    assert event_display_name("SmartMotionHuman") == "Smart Motion Human"
    assert event_display_name("IVS") == "IVS"
    assert event_display_name("NTPAdjustTime") == "NTPAdjust Time"
    assert event_display_name("MDResult") == "MDResult"
