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
"""

import json
import pathlib

EN = (pathlib.Path(__file__).parents[2]
      / "custom_components" / "dahua" / "translations" / "en.json")


def _labels():
    strings = json.loads(EN.read_text(encoding="utf-8"))
    out = []
    for section in ("config", "options"):
        steps = strings.get(section, {}).get("step", {})
        for step in steps.values():
            label = step.get("data", {}).get("channel")
            if label:
                out.append(label)
    return out


def test_both_forms_describe_the_channel():
    labels = _labels()

    assert len(labels) == 2, "expected the add and the reconfigure forms"


def test_the_two_forms_say_the_same_thing():
    """Two different answers to the same question is worse than one."""
    first, second = _labels()

    assert first == second


def test_the_label_relates_the_index_to_what_the_recorder_shows():
    """"0 based index" alone is true and still lets someone type 4 for channel 4."""
    label = _labels()[0]

    assert "0-based index" in label or "0 based index" in label
    assert "recorder" in label.lower(), (
        "the label must connect the index to the number the NVR displays")
