"""The channel field must say what the number means.

The field is a 0-based index and the integration adds one before talking to the
device. Measured on a DHI-NVR5464-16P-EI:

    snapshot.cgi?channel=0   400 Bad Request
    snapshot.cgi?channel=1   200, a real image
    snapshot.cgi?channel=2   200, a real image

So a recorder's channel 4 is 3 in this form. Every NVR UI numbers its channels
from 1, so "the 0 based index" is accurate and still lets someone type the
number their recorder shows -- which is a different camera, or none. #646 is
someone who could add two of their three cameras and never found the third.

The explanation used to live in the field's *label*, which is why that label ran
to 130 characters and rendered as a paragraph where a field name should be. It
now lives in `data_description`, which Home Assistant renders as helper text
under the field. What has to hold is that the user is told, not which key it
sits in, so these tests read both.
"""

import json
import pathlib
import re

EN = (pathlib.Path(__file__).parents[2]
      / "custom_components" / "dahua" / "translations" / "en.json")


def _channel_text():
    """(label, helper) for the channel field of every form that has one."""
    strings = json.loads(EN.read_text(encoding="utf-8"))
    out = []
    for section in ("config", "options"):
        steps = strings.get(section, {}).get("step", {})
        for step in steps.values():
            label = step.get("data", {}).get("channel")
            if not label:
                continue
            out.append((label, step.get("data_description", {}).get("channel", "")))
    return out


def test_both_forms_describe_the_channel():
    assert len(_channel_text()) == 2, "expected the add and the reconfigure forms"


def test_the_two_forms_say_the_same_thing():
    """Two different answers to the same question is worse than one."""
    first, second = _channel_text()

    assert first == second


def test_the_label_is_a_field_name_not_a_paragraph():
    """A 130 character label is a sign the explanation has nowhere to go."""
    for label, _helper in _channel_text():
        assert len(label) <= 40, (
            "the explanation belongs in data_description, not the label")


def test_the_user_is_told_how_the_index_relates_to_the_recorder():
    """"0 based index" alone is true and still lets someone type 4 for channel 4,
    so the worked example is the part that actually helps.

    The relation and the example have to be in the SAME sentence. Asserting that
    "recorder" and the example each appear somewhere is too weak: this text also
    says "The recorder's other channels can be added on the next screen", which
    satisfies a bare "recorder" check on its own. A mutation that deleted the
    relation and kept that sentence survived until this was tightened.
    """
    for label, helper in _channel_text():
        shown = "%s %s" % (label, helper)

        assert "0" in shown, "must say the count starts at 0"

        sentences = re.split(r"(?<=[.])\s+", shown)
        assert any("recorder" in one.lower() and "4" in one and "3" in one
                   for one in sentences), (
            "one sentence must relate the number the recorder shows to the index, "
            "e.g. \"a recorder's channel 4 is 3 here\"")


def test_the_explanation_is_where_home_assistant_will_render_it():
    """A data_description key only renders if the field exists in data."""
    for label, helper in _channel_text():
        assert helper, "the channel field has no helper text"


def test_every_field_on_the_add_form_is_explained():
    """The Bronze `config-flow` rule asks for data_description on the fields, and
    this form is the one where people get stuck."""
    strings = json.loads(EN.read_text(encoding="utf-8"))
    step = strings["config"]["step"]["user"]

    missing = set(step["data"]) - set(step.get("data_description", {}))

    assert not missing, "no helper text for %s" % sorted(missing)


def test_no_helper_text_describes_a_field_that_does_not_exist():
    """A stale data_description key renders nowhere and misleads the next reader,
    which is how `streams` survived in ten translation files."""
    strings = json.loads(EN.read_text(encoding="utf-8"))
    for section in ("config", "options"):
        for name, step in strings.get(section, {}).get("step", {}).items():
            orphans = set(step.get("data_description", {})) - set(step.get("data", {}))
            assert not orphans, "%s.%s describes missing fields %s" % (
                section, name, sorted(orphans))


def test_the_password_field_says_it_is_not_a_cloud_account():
    """The commonest human cause of "credentials refused although correct": the
    Dahua or Imou app login is not the device's own account."""
    strings = json.loads(EN.read_text(encoding="utf-8"))
    helpers = strings["config"]["step"]["user"]["data_description"]

    shown = "%s %s" % (helpers["username"], helpers["password"])

    assert "cloud" in shown.lower()
