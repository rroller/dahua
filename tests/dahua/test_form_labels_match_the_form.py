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

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from custom_components.dahua.config_flow import (
    CHANNEL_SUBENTRY,
    DahuaChannelSubentryFlow,
    DahuaFlowHandler,
    OPTIONS_SECTION_PLATFORMS,
)
from custom_components.dahua.const import PLATFORMS

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


# --- the options form's platform toggles, which are built from a constant ------

def _section(step="user", language="en"):
    """The strings for the collapsed platform section, if a language has any."""
    path = TRANSLATIONS / ("%s.json" % language)
    body = json.loads(path.read_text(encoding="utf-8"))["options"]["step"]
    return body.get(step, {}).get("sections", {}).get(
        OPTIONS_SECTION_PLATFORMS, {})


def _platform_labels(language="en"):
    return set(_section(language=language).get("data", {}))


def _options_data(language):
    """The keys at the options step's own `data`, outside any section."""
    path = TRANSLATIONS / ("%s.json" % language)
    body = json.loads(path.read_text(encoding="utf-8")).get("options", {})
    return set(body.get("step", {}).get("user", {}).get("data", {}))


def test_every_platform_toggle_has_a_label():
    """The toggles are `{vol.Required(x): bool for x in sorted(PLATFORMS)}`, so a
    platform added to that constant grows a checkbox whether or not anybody wrote a
    string for it.

    The frontend looks a section's child up at
    `options.step.<step>.sections.<section>.data.<field>` and at nothing else, and
    renders the raw key when that misses. So the new platform appears to the user as
    the word `number`, in every language, because English is the per-key fallback and
    English would not have it either.
    """
    missing = set(PLATFORMS) - _platform_labels()

    assert not missing, "no label for the %s toggle" % sorted(missing)


@pytest.mark.parametrize("language", ["en", "bg", "ca", "es", "fr", "it", "nl",
                                      "pt", "pt-BR"])
def test_no_language_labels_a_platform_that_does_not_exist(language):
    """The orphan direction, which is the half #814 was about: a string for a
    platform that is no longer in `PLATFORMS` renders nowhere at all."""
    orphans = _platform_labels(language) - set(PLATFORMS)

    assert not orphans, "%s labels the %s toggle, which is not a platform" % (
        language, sorted(orphans))


@pytest.mark.parametrize("language", ["en", "bg", "ca", "es", "fr", "it", "nl",
                                      "pt", "pt-BR"])
def test_no_language_labels_a_platform_outside_the_section(language):
    """A platform label at the step's own `data` is read by nothing.

    The toggles were the first eight fields of the options form before they moved
    into the collapsed section, and eight of the nine files kept their six
    translated labels at the old path. Those installs showed the English labels
    while their own translations sat unread, which is worse than an untranslated
    string because it looks like nobody had written one.

    Scoped to the platform keys deliberately. `options.step.user.data` is a live
    path that legitimately holds `scan_interval`, `events`, `area` and the rest, so
    the rule is about which keys belong in the section, not about that dict being
    empty."""
    stranded = _options_data(language) & set(PLATFORMS)

    assert not stranded, (
        "%s labels %s at options.step.user.data, where a section's child is never "
        "looked up" % (language, sorted(stranded)))


def test_the_platform_section_has_a_heading_of_its_own():
    """A section's heading comes from `sections.<name>.name`, and falls back to the
    raw key exactly as a field does, so the group would be titled `platforms`."""
    section = _section()

    assert section.get("name"), "the platform section has no heading"
    assert section.get("description"), "the platform section has no description"


def test_every_description_in_the_section_describes_a_toggle_in_it():
    """`sections.<name>.data_description.<field>` is read only for a field in that
    same section, so one naming a field elsewhere renders nowhere."""
    described = set(_section().get("data_description", {}))

    orphans = described - _platform_labels()

    assert not orphans, "the section describes %s" % sorted(orphans)


# --- the per channel form, which is a third place labels can go missing -------

def _subentry_step(step="reconfigure", language="en"):
    path = TRANSLATIONS / ("%s.json" % language)
    body = json.loads(path.read_text(encoding="utf-8"))
    return (body.get("config_subentries", {}).get(CHANNEL_SUBENTRY, {})
            .get("step", {}).get(step, {}))


def _subentry_schema_fields():
    """The keys the per channel form offers.

    The schema is built from no instance state beyond the subentry it is editing,
    so a bare handler with a stub subentry is enough and this needs no Home
    Assistant.
    """
    flow = DahuaChannelSubentryFlow()
    flow._get_reconfigure_subentry = lambda: SimpleNamespace(
        data={"channel": 3}, title="A channel")
    result = asyncio.run(flow.async_step_reconfigure())
    return {str(key) for key in result["data_schema"].schema}


def test_every_field_on_the_channel_form_has_a_label():
    """Read out of the frontend: a subentry field is looked up at
    `config_subentries.<type>.step.<step>.data.<field>` and falls back to the raw
    key, exactly as the other two forms do. So an unlabelled one appears to the
    user as `nvr_active_deterrence`."""
    missing = _subentry_schema_fields() - set(_subentry_step().get("data", {}))

    assert not missing, "no label for %s on the channel form" % sorted(missing)


def test_the_channel_form_labels_no_field_it_does_not_ask():
    orphans = set(_subentry_step().get("data", {})) - _subentry_schema_fields()

    assert not orphans, "the channel form labels %s, which it does not ask" % sorted(
        orphans)


def test_every_description_on_the_channel_form_describes_a_field_it_asks():
    described = set(_subentry_step().get("data_description", {}))

    orphans = described - _subentry_schema_fields()

    assert not orphans, "describes %s" % sorted(orphans)


def test_the_channel_form_has_a_title_and_a_description():
    """A step with neither renders as the integration's name and nothing else,
    which on a 64 channel recorder gives no clue which channel is being edited."""
    step = _subentry_step()

    assert step.get("title")
    assert step.get("description")


def test_a_setting_that_moved_onto_the_channel_kept_its_wording():
    """These were on the options screen before #827 moved them per channel. A
    setting acquiring a second name because it moved is how a user ends up unable
    to find the thing a forum post told them to tick."""
    channel = _subentry_step().get("data", {})
    options = EN["options"]["step"]["user"].get("data", {})

    shared = set(channel) & set(options)
    assert shared, "expected some settings to be common to both forms"
    for key in sorted(shared):
        assert channel[key] == options[key], (
            "%s is worded differently on the two forms: %r vs %r"
            % (key, channel[key], options[key]))
