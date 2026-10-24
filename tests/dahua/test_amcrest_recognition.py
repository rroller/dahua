"""The same device, recognised the same way by both questions that ask.

Two checks are about the same Amcrest doorbells and disagreed:

    is_amcrest_doorbell                    self.model.upper().startswith("AD") / ("DB6")
    supports_smart_motion_detection_amcrest  self.model == "AD410" or "DB61i"

One folds case and takes a prefix; the other compared the raw string exactly.
So a doorbell reporting `DB61I` rather than `DB61i` was a doorbell to one and
not the other.

Falling through the second is not merely a missing switch. Both the state read
and the write take the non-Amcrest branch, which uses SmartMotionDetect -- a
table an Amcrest doorbell does not have -- so the entity reports nothing and the
device's IVS rule is never touched.
"""

import pytest

from custom_components.dahua import DahuaDataUpdateCoordinator


def _coordinator(model):
    c = object.__new__(DahuaDataUpdateCoordinator)
    c.model = model
    return c


# --- the two checks must agree about a device --------------------------------


@pytest.mark.parametrize(
    "model",
    [
        "AD410",
        "ad410",
        "AD410 ",
        "DB61i",
        "DB61I",
        "db61i",
    ],
)
def test_a_doorbell_is_a_doorbell_to_both_questions(model):
    c = _coordinator(model)

    assert c.is_amcrest_doorbell(), "not recognised as an Amcrest doorbell"
    assert (
        c.supports_smart_motion_detection_amcrest()
    ), "recognised as a doorbell but not for its own smart motion path"


# --- the cases that used to fall through -------------------------------------


def test_the_uppercase_i_is_the_same_doorbell():
    """DB61i and DB61I are one device, and one spelling used to lose its switch."""
    assert _coordinator("DB61I").supports_smart_motion_detection_amcrest()


def test_trailing_whitespace_does_not_change_the_device():
    assert _coordinator("AD410 ").supports_smart_motion_detection_amcrest()


def test_the_documented_spellings_still_match():
    """Whatever else changes, the two values this was written for must hold."""
    assert _coordinator("AD410").supports_smart_motion_detection_amcrest()
    assert _coordinator("DB61i").supports_smart_motion_detection_amcrest()


# --- and nothing else may match ----------------------------------------------


@pytest.mark.parametrize(
    "model",
    [
        "IPC-HDW3849HP-AS-PV",
        "DHI-NVR5464-16P-EI",
        "VTO2202F-P-S2",
        "N843A8",
        "",
    ],
)
def test_a_device_that_is_not_one_of_these_is_not_claimed(model):
    assert not _coordinator(model).supports_smart_motion_detection_amcrest()


def test_a_different_db_model_is_not_claimed():
    """DB6 is the doorbell family prefix; DB61 is this specific device.

    is_amcrest_doorbell is deliberately broader than this one, so a DB62 would
    be a doorbell without being claimed for the Amcrest smart motion path.
    """
    c = _coordinator("DB62X")

    assert c.is_amcrest_doorbell()
    assert not c.supports_smart_motion_detection_amcrest()


# --- a vendor prefix is the same doorbell (#1131) ----------------------------
#
# @phabibjr reported a DH-DB6I on firmware 1.000.020000000.9.R where none of
# the four doorbell sensors existed and pressing the button did nothing
# visible. The model is the DB6 family with a `DH-` vendor prefix on the
# front, and a prefix-only test does not see it. `is_doorbell` already allows
# for exactly this on VTO, with `-VTO` and `_VTO` accepted anywhere in the
# string; this is the same rule for the same reason.


@pytest.mark.parametrize(
    "model",
    [
        "DH-DB6I",
        "dh-db6i",
        "DHI-DB6I",
        "DH_DB6I",
        "DH-DB61i",
    ],
)
def test_a_vendor_prefixed_doorbell_is_still_a_doorbell(model):
    c = _coordinator(model)

    assert c.is_amcrest_doorbell(), "a vendor prefix hid the doorbell"
    assert c.is_doorbell(), "not a doorbell, so it gets no doorbell sensors"


def test_the_model_decides_it_when_the_device_names_no_class():
    """The reported firmware is the case where the model list is all there is.

    `is_doorbell` asks the device first, and a device answering `VTO` is one
    whatever its model says. A coordinator that never got an answer has no
    `_device_class` at all, which is the state #1131 was reported in, so the
    model match has to carry it alone.
    """
    c = _coordinator("DH-DB6I")

    assert not hasattr(c, "_device_class")
    assert c.is_doorbell()


@pytest.mark.parametrize(
    "model",
    [
        "XDB6",
        "IPC-HDW3849HP-AS-PV",
        "DHI-NVR5464-16P-EI",
        "",
    ],
)
def test_db6_without_a_separator_is_not_a_doorbell(model):
    """The infix needs a `-` or `_` in front of it, not DB6 anywhere at all."""
    assert not _coordinator(model).is_amcrest_doorbell()


def test_a_vendor_prefix_does_not_claim_the_smart_motion_path():
    """The asymmetry above is deliberate and must survive this change.

    DB6 is the family and DB61 is the one device whose Amcrest smart motion
    path has been measured. A DH-DB6I is a doorbell without being claimed for
    that path, exactly as a DB62X is.
    """
    c = _coordinator("DH-DB6I")

    assert c.is_amcrest_doorbell()
    assert not c.supports_smart_motion_detection_amcrest()


# --- the device's own answer, for this family too (#1131) --------------------
#
# @phabibjr measured `getDeviceClass` on the DH-DB6I: HTTP 200, `class=DB`.
# So it does name itself, and it names itself something the VTO check did not
# recognise. That answer is better evidence than any model string, and it is
# the thing that keeps working when the next rebadge arrives.


def _classed(model, device_class):
    c = _coordinator(model)
    c._device_class = device_class
    return c


def test_a_device_that_says_db_is_a_doorbell():
    """Measured on the DH-DB6I: class=DB over digest, HTTP 200."""
    assert _classed("DH-DB6I", "DB").is_doorbell()


def test_db_carries_a_model_the_list_would_miss():
    """The point of asking the device: the name no longer has to be known."""
    assert _classed("SOMETHING-NOBODY-HAS-SEEN", "DB").is_doorbell()


def test_vto_still_answers_for_itself():
    assert _classed("SOMETHING-NOBODY-HAS-SEEN", "VTO").is_doorbell()


@pytest.mark.parametrize("device_class", ["NVR", "IPC", "VTH", ""])
def test_another_class_does_not_make_a_camera_a_doorbell(device_class):
    """Additive, not a verdict: a non-doorbell class falls through to the list."""
    assert not _classed("IPC-HDW3849HP-AS-PV", device_class).is_doorbell()


def test_a_class_that_is_not_a_doorbell_cannot_veto_the_model():
    """A device answering IPC whose model is a doorbell is still a doorbell.

    The class check only ever adds. If it started deciding, one wrong answer
    from a rebadge would remove every doorbell entity that works today.
    """
    assert _classed("DH-DB6I", "IPC").is_doorbell()
