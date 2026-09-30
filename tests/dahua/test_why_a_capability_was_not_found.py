"""What the diagnostics say about a capability, which plate counts, and which channel
controls the light.

Three readers whose whole purpose is to be read by a person, and which were never
executed.

**`get_siren_detection_sources` and `get_security_light_detection_sources` exist to
answer "why does my camera not have a siren?".** #848 asked for exactly that. They return
the positive evidence when there is any, and otherwise the list of checks that failed, so
a diagnostics download says whether the device denied the capability or whether nothing
ever asked. The line that says *nothing ever asked* was the uncovered one, which is the
worst one to lose: without it an unprobed device and a device that answered no look
identical in the dump, and that is the confusion
[[observability-fields-go-dead-silently]] is about.

These also carry the **model-name whitelists**, which are this integration's recurring
bug class: #570, #676 and #690 were each a capability gated on a model prefix rather than
on what the device reported. The fallbacks are tested here by name so that removing one
is a red test rather than a silent loss of support for that model.

**`get_security_light_control_channel` routes a write three ways** and the wrong answer
turns on a different camera's light, exactly as in the flood light case. Recorder
deterrence uses the channel *number*, one PTZ model is hard-wired to 1, and everything
else uses the channel index.

**`is_plate_authorized` matches zero against the letter O**, because a plate read is an
OCR result and those two are the pair it gets wrong. A test is the only place that
intent is written down; from the code alone `replace("0", "O")` looks like a typo.
"""

from types import SimpleNamespace

import pytest

from custom_components.dahua import DahuaDataUpdateCoordinator

CHANNEL_INDEX = 5
CHANNEL_NUMBER = 6


def _coordinator(**attrs):
    coordinator = object.__new__(DahuaDataUpdateCoordinator)
    coordinator.model = ""
    coordinator._channel = CHANNEL_INDEX
    coordinator._channel_number = CHANNEL_NUMBER
    coordinator.is_doorbell = lambda: False
    coordinator.uses_recorder_deterrence = lambda: False
    coordinator.is_ptz3e10x_t180 = lambda: False
    for name, value in attrs.items():
        setattr(coordinator, name, value)
    return coordinator


# --- why a capability was not found -----------------------------------------

SOURCES = ["get_siren_detection_sources", "get_security_light_detection_sources"]


@pytest.mark.parametrize("getter", SOURCES)
def test_an_unprobed_device_says_so_rather_than_saying_nothing(getter):
    """The uncovered line, and the one that matters most. With no evidence and no
    recorded failures the answer must still explain itself, or a device that was never
    probed reads in the dump exactly like one that answered no."""
    answer = getattr(_coordinator(), getter)()

    assert any("has not run" in line for line in answer), answer


@pytest.mark.parametrize("getter", SOURCES)
def test_the_failure_list_names_all_three_checks(getter):
    """A dump that lists one reason invites a wrong conclusion about the other two."""
    answer = getattr(_coordinator(), getter)()

    assert any("has not run" in line for line in answer), answer
    assert any("Model fallback" in line for line in answer), answer
    assert any("Manual override" in line for line in answer), answer


@pytest.mark.parametrize("getter, attribute", [
    ("get_siren_detection_sources", "_siren_detection_failures"),
    ("get_security_light_detection_sources", "_security_light_detection_failures"),
])
def test_a_recorded_failure_is_not_overwritten_by_the_generic_line(getter, attribute):
    """Real recorded evidence is more specific than "has not run", so it wins."""
    answer = getattr(_coordinator(**{attribute: ["RPC2 refused getCaps"]}), getter)()

    assert "RPC2 refused getCaps" in answer
    assert not any("has not run" in line for line in answer), answer


@pytest.mark.parametrize("getter, attribute", [
    ("get_siren_detection_sources", "_siren_detection_sources"),
    ("get_security_light_detection_sources", "_security_light_detection_sources"),
])
def test_positive_evidence_is_returned_instead_of_the_failures(getter, attribute):
    answer = getattr(_coordinator(**{attribute: ["ProductDefinition says yes"]}), getter)()

    assert answer == ["ProductDefinition says yes"]


# --- the model whitelists, named so that losing one is a red test ------------


@pytest.mark.parametrize("model", [
    "IPC-HDW3849HP-AS-PV", "IP8M-2796E-something", "IPC-COLOR4M-TZ-A",
    "PTZ3E10X-T180", "AD410", "DB61I",
])
def test_a_whitelisted_model_reports_its_security_light_fallback(model):
    """PTZ3E10X-T180 was the uncovered one. These are the bug class of #570, #676 and
    #690, so each is pinned by name rather than by "some model matched"."""
    answer = _coordinator(model=model).get_security_light_detection_sources()

    assert any("Model fallback" in line for line in answer), (model, answer)
    assert not any("no matching" in line for line in answer), (model, answer)


@pytest.mark.parametrize("model", [
    "IPC-HDW3849HP-AS-PV", "TPC-BF1241-something", "W452ASD-x", "DH-L46N",
])
def test_a_whitelisted_model_reports_its_siren_fallback(model):
    answer = _coordinator(model=model).get_siren_detection_sources()

    assert any("Model fallback" in line for line in answer), (model, answer)
    # Required, not belt and braces: the *failure* line also begins "Model fallback",
    # so without this the assertion above holds for a model that matched nothing.
    assert not any("no matching" in line for line in answer), (model, answer)


def test_the_model_is_matched_without_regard_to_case():
    """`model.upper()` is applied first, and devices are inconsistent about it."""
    answer = _coordinator(model="ad410").get_security_light_detection_sources()

    assert any("Model fallback: AD410" in line for line in answer), answer
    assert not any("no matching" in line for line in answer), answer


def test_an_unlisted_model_says_no_model_matched():
    answer = _coordinator(model="IPC-HFW1234").get_security_light_detection_sources()

    assert any("no matching" in line for line in answer), answer


# --- the manual override, and the doorbell exception ------------------------


def test_a_manual_override_is_reported_as_evidence():
    answer = _coordinator(_manual_siren=True).get_siren_detection_sources()

    assert any("manual_siren=true" in line for line in answer), answer


def test_a_doorbell_is_told_why_its_override_was_ignored():
    """The override is excluded for doorbells, and a user who set it deserves to see
    that rather than a bare "disabled"."""
    answer = _coordinator(
        _manual_siren=True, is_doorbell=lambda: True).get_siren_detection_sources()

    assert any("excluded for doorbell" in line for line in answer), answer


def test_no_override_is_reported_as_disabled():
    answer = _coordinator().get_siren_detection_sources()

    assert any("Manual override: disabled" in line for line in answer), answer


# --- which channel controls the light ---------------------------------------


def test_recorder_deterrence_controls_by_channel_number():
    coordinator = _coordinator(uses_recorder_deterrence=lambda: True)

    assert coordinator.get_security_light_control_channel() == CHANNEL_NUMBER


def test_the_ptz_model_is_hard_wired_to_channel_one():
    coordinator = _coordinator(is_ptz3e10x_t180=lambda: True)

    assert coordinator.get_security_light_control_channel() == 1


def test_everything_else_controls_by_channel_index():
    assert _coordinator().get_security_light_control_channel() == CHANNEL_INDEX


def test_the_recorder_wins_over_the_ptz_model():
    """Both can be true on a recorder with that camera attached, and the order decides
    which write shape is used."""
    coordinator = _coordinator(
        uses_recorder_deterrence=lambda: True, is_ptz3e10x_t180=lambda: True)

    assert coordinator.get_security_light_control_channel() == CHANNEL_NUMBER


# --- which plate counts as authorized ---------------------------------------


def _plates(*authorized):
    return _coordinator(get_authorized_plates=lambda: list(authorized))


def test_an_exact_plate_is_authorized():
    assert _plates("ABC123").is_plate_authorized("ABC123") is True


def test_an_unlisted_plate_is_not():
    assert _plates("ABC123").is_plate_authorized("XYZ789") is False


@pytest.mark.parametrize("plate", [None, "", "unknown"])
def test_nothing_recognised_is_never_authorized(plate):
    """`"unknown"` is what `get_last_plate` returns when no plate has been read, so it
    must not be allowed to match an authorized entry.

    The authorized entry here is spelled `UNKNOWN` deliberately. Configured plates are
    stored normalised and `normalize_plate` upper-cases, so a lower case entry could
    never have matched and the guard would not have been what made this pass.
    """
    assert _plates("ABC123", "UNKNOWN").is_plate_authorized(plate) is False


def test_zero_matches_the_letter_o_both_ways():
    """The uncovered line. A plate read is OCR, and 0 against O is the pair it gets
    wrong, so the comparison is made with both sides folded the same way. From the
    code alone `replace("0", "O")` reads like a mistake; this is where the intent is."""
    assert _plates("ABCO").is_plate_authorized("ABC0") is True
    assert _plates("ABC0").is_plate_authorized("ABCO") is True


def test_the_fold_does_not_make_everything_match():
    """The guard on the test above. A fold that was too eager would authorise any
    plate of the right length."""
    assert _plates("ABCO").is_plate_authorized("ABC1") is False
    assert _plates("ABCO").is_plate_authorized("XYZO") is False


# --- and the plain readers --------------------------------------------------


def test_the_plate_metadata_is_a_dict_even_before_one_is_read():
    """Callers index into it, so `None` here would be a TypeError on a fresh device."""
    assert _coordinator(_last_plate_data=None).get_last_plate_data() == {}
    assert _coordinator(
        _last_plate_data={"plate": "A"}).get_last_plate_data() == {"plate": "A"}


def test_the_plate_timestamp_and_reboot_generation_are_surfaced():
    assert _coordinator(_last_plate_timestamp=1620477656).get_last_plate_timestamp() \
        == 1620477656
    assert _coordinator(_camera_reboot_generation=2).get_camera_reboot_generation() == 2


def test_an_event_that_has_never_fired_has_timestamp_zero():
    """Binary sensors subtract this from now, so `None` would be a TypeError and a
    missing key has to read as "not recently"."""
    coordinator = _coordinator(
        _dahua_event_timestamp={},
        get_event_key=lambda name: "key_%s" % name)

    assert coordinator.get_event_timestamp("CrossLineDetection") == 0


def test_an_event_that_has_fired_reports_when():
    coordinator = _coordinator(
        _dahua_event_timestamp={"key_VideoMotion": 1620477656},
        get_event_key=lambda name: "key_%s" % name)

    assert coordinator.get_event_timestamp("VideoMotion") == 1620477656
