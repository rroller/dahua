"""Every field the add form offers should have a label, and no label should be an orphan.

`config.step.user.data` carried an `events` label for three weeks after the field itself
was taken off the form. #796 moved the event selection to the options screen and #802 cut
the add form to four questions, and between them nothing was left reading that string.

An orphan renders nowhere, so nothing fails and nobody notices. The opposite case is the
one that shows: a field with no label renders as its raw key, so a new question added to
the schema without a string appears to the user as `rtsp_port`.

Both are the same mistake, and both are decidable from the code, which is the point of
this file. The alternative is a hand-maintained pairing, and that is exactly how the
issue placeholder map quietly stopped covering a new issue.
"""

import json
from pathlib import Path

import pytest

from custom_components.dahua.config_flow import DahuaFlowHandler

TRANSLATIONS = (Path(__file__).resolve().parents[2]
                / "custom_components" / "dahua" / "translations")

EN = json.loads((TRANSLATIONS / "en.json").read_text(encoding="utf-8"))


def _labels(step):
    return set(EN["config"]["step"][step].get("data", {}))


def _described(step):
    return set(EN["config"]["step"][step].get("data_description", {}))


def _schema_fields(reveal_transport):
    """The keys the add form actually offers.

    `_user_schema` reads no instance state, so a bare handler is enough and this needs
    no Home Assistant.
    """
    schema = DahuaFlowHandler()._user_schema(reveal_transport=reveal_transport)
    return {str(key) for key in schema.schema}


# --- the add form ----------------------------------------------------------

def test_every_field_the_add_form_can_show_has_a_label():
    """A field with no string renders as its raw key, which is what the channel field
    looked like before #796."""
    missing = _schema_fields(reveal_transport=True) - _labels("user")

    assert not missing, "no label for %s" % sorted(missing)


def _languages():
    return sorted(p.stem for p in TRANSLATIONS.glob("*.json"))


def _labels_in(language, step="user"):
    path = TRANSLATIONS / ("%s.json" % language)
    body = json.loads(path.read_text(encoding="utf-8"))["config"]["step"].get(step, {})
    return set(body.get("data", {}))


@pytest.mark.parametrize("language", ["en", "bg", "ca", "es", "fr", "it", "nl",
                                      "pt", "pt-BR"])
def test_no_language_labels_a_field_the_add_form_does_not_offer(language):
    """The `events` label outlived its field by three weeks, in all nine files. The
    transport fields count as offered because they are revealed on a failure rather
    than absent.

    Only the orphan direction generalises. Home Assistant falls back per key, so a
    language that is missing `use_https` renders the English string and is merely
    untranslated, which is ordinary debt rather than a fault."""
    orphans = _labels_in(language) - _schema_fields(reveal_transport=True)

    assert not orphans, "%s labels %s, which the form does not ask" % (
        language, sorted(orphans))


def test_the_language_list_above_is_the_list_that_ships():
    """Derived rather than trusted: a language added without being listed would
    silently stop being checked, which is how the issue placeholder map quietly
    stopped covering a new issue."""
    assert set(_languages()) == {"en", "bg", "ca", "es", "fr", "it", "nl",
                                 "pt", "pt-BR"}


def test_the_hidden_transport_fields_still_have_labels():
    """They are off the form until something fails, and that is the moment somebody
    needs to read them, so a missing string there is worse than usual."""
    revealed = _schema_fields(True) - _schema_fields(False)

    assert revealed, "the transport fields are no longer conditional"
    assert revealed <= _labels("user")


def test_every_description_describes_a_field_that_exists():
    """`data_description` renders under its field, so one with no field renders
    nowhere at all."""
    orphans = _described("user") - _labels("user")

    assert not orphans, "description with no field: %s" % sorted(orphans)


# --- and the events label belongs to the options form, which does have one ---

def test_the_options_form_keeps_its_events_label():
    """Removing it from the add form must not be read as removing it everywhere: the
    options screen is where the event selection went, and that field is real."""
    assert "events" in EN["options"]["step"]["user"]["data"]


def test_the_add_form_does_not_ask_about_events():
    """The reason the label went. Asserted against the schema rather than the strings,
    so this states the behaviour and not the cleanup."""
    assert "events" not in _schema_fields(reveal_transport=True)


# --- the other steps, where the pairing is simple enough to state -----------

@pytest.mark.parametrize("step", ["name", "reauth_confirm", "reconfigure"])
def test_no_step_describes_a_field_it_does_not_declare(step):
    orphans = _described(step) - _labels(step)

    assert not orphans, "%s describes %s" % (step, sorted(orphans))


def test_the_areas_step_declares_no_fields_on_purpose():
    """Its keys are the channel labels the recorder reported, because Home Assistant
    renders a key verbatim when no string exists. That is what lets a form whose fields
    depend on the device read properly without inventing a key per channel."""
    assert _labels("areas") == set()
