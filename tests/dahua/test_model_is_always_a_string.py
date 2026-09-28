"""self.model must be a string, whatever the device answered.

Every capability check does `self.model.upper()` or a substring test, and the
attribute is initialised to "". The model is resolved from three sources in
order of how specific they are:

    deviceType  -- but recorders answer a number (Lorex sends 31) or nothing
    updateSerial -- where those recorders actually put it
    getDeviceType -- asked only when the first two gave nothing

#59 was a DVR that omitted deviceType, and setup died with

    File "__init__.py", line 411, in is_doorbell
        m = self.model.upper()
    AttributeError: 'NoneType' object has no attribute 'upper'

That was fixed by adding the two fallbacks. But the last one can itself come
back empty -- getDeviceType answering an empty body, or an error string with no
"=" in it, leaves .get("type") as None -- so the same crash was still reachable
by a different road, and a device calling itself "IP Camera" was thrown away for
None the moment the more specific lookups failed.
"""

import pytest

from custom_components.dahua import model_name


# --- the ordinary path: something specific was found ------------------------

def test_a_specific_model_wins():
    assert model_name("DHI-NVR5464-16P-EI", "IP Camera") == "DHI-NVR5464-16P-EI"


def test_the_dvr_from_59_gets_its_model_from_update_serial():
    """updateSerial=XVR5108C-X, no deviceType at all."""
    assert model_name("XVR5108C-X", None) == "XVR5108C-X"


def test_whitespace_is_not_part_of_the_model():
    """getDeviceType answers `type=DH-XVR5108C-X ` with a trailing space (#59)."""
    assert model_name("DH-XVR5108C-X ", None) == "DH-XVR5108C-X"


# --- the roads that used to end in None -------------------------------------

def test_an_empty_lookup_keeps_the_generic_value_rather_than_losing_it():
    """"IP Camera" is not useful, but it is a great deal better than None."""
    assert model_name(None, "IP Camera") == "IP Camera"


def test_a_device_that_answers_nothing_useful_gets_no_model():
    assert model_name(None, None) == ""


@pytest.mark.parametrize("resolved", [None, "", "   "])
def test_the_result_is_never_none(resolved):
    """The crash in #59 was an attribute error on None, not a wrong string."""
    result = model_name(resolved, None)

    assert isinstance(result, str)
    result.upper()  # what is_doorbell does, and what used to raise


def test_no_model_leaves_capability_checks_false_rather_than_raising():
    """The behaviour "" buys: every model gate reads as no match."""
    model = model_name(None, None)

    assert not model.upper().startswith("AD")
    assert "-AS-PV" not in model
    assert "NVR" not in model.upper()
